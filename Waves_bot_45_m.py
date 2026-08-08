import os
import sys
import time
import json
import math
import threading
from datetime import datetime

import numpy as np
import pandas as pd
from pybit.unified_trading import HTTP

# Импорты Kivy для Android UI
from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.spinner import Spinner
from kivy.uix.popup import Popup
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem

# Matplotlib integration for Kivy
import matplotlib
matplotlib.use('module://kivy.garden.matplotlib.backend_kivy')
from matplotlib.figure import Figure
from kivy.garden.matplotlib.backend_kivyagg import FigureCanvasKivyAgg

# --- ИЗОЛЯЦИЯ КОНФИГУРАЦИИ ДЛЯ ANDROID ---
DEFAULT_CONFIG_NAME = "bot_config_45m.json"
CONFIG_FILE = DEFAULT_CONFIG_NAME
PROCESS_PID = os.getpid()

def calculate_smma(series, period):
    smma_vals = np.zeros(len(series))
    if len(series) < period:
        return pd.Series(smma_vals, index=series.index)
        
    smma_vals[period - 1] = series.iloc[:period].mean()
    for i in range(period, len(series)):
        smma_vals[i] = (smma_vals[i - 1] * (period - 1) + series.iloc[i]) / period
    
    smma_vals[:period - 1] = np.nan
    return pd.Series(smma_vals, index=series.index)


def calculate_alligator(df):
    if df.empty or len(df) < 25:
        empty_s = pd.Series(dtype=float)
        return empty_s, empty_s, empty_s

    median_price = (df['highPrice'] + df['lowPrice']) / 2.0

    jaw = calculate_smma(median_price, 13).shift(8)
    teeth = calculate_smma(median_price, 8).shift(5)
    lips = calculate_smma(median_price, 5).shift(3)

    return jaw, teeth, lips


class TradingBotThread(threading.Thread):
    def __init__(self, symbol, api_key, api_secret, log_callback):
        super().__init__()
        self.symbol = symbol
        self.api_key = api_key
        self.api_secret = api_secret
        self.log_callback = log_callback
        self.running = True
        self.trading_active = False
        self.daemon = True

        self.timeframe = "60"  
        self.air_bag_pct = 2.0
        self.leverage = 1
        self.category = "linear"
        self.position_size_usdt = 300.0
        
        self.current_price = 0.0
        self.pos_side = "НЕТ"
        self.pos_size = 0.0
        self.pos_value_usdt = 0.0 
        self.entry_price = 0.0
        self.sl_price = 0.0
        self.tp_price = 0.0  
        self.daily_pnl = 0.0       
        self.status = "Мониторинг"
        self.klines_df = pd.DataFrame()
        self.is_executing = False
        
        self.breakeven_triggered = False
        self.session = HTTP(demo=True, api_key=self.api_key, api_secret=self.api_secret)

    def log(self, message):
        self.log_callback(f"[{self.symbol}] {message}")

    def update_keys(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = HTTP(demo=True, api_key=self.api_key, api_secret=self.api_secret)

    def get_market_precision(self):
        try:
            res = self.session.get_instruments_info(category=self.category, symbol=self.symbol)
            if res.get("retCode") == 0:
                info_list = res.get("result", {}).get("list", [])
                if info_list:
                    lot_filter = info_list[0].get("lotSizeFilter", {})
                    price_filter = info_list[0].get("priceFilter", {})
                    return {
                        "qty_step": float(lot_filter.get("qtyStep", 1.0)),
                        "min_qty": float(lot_filter.get("minOrderQty", 1.0)),
                        "tick_size": float(price_filter.get("tickSize", 0.01))
                    }
        except Exception:
            pass
        
        if "1000" in self.symbol:
            return {"qty_step": 1.0, "min_qty": 100.0, "tick_size": 0.000001}
        elif "BTC" in self.symbol:
            return {"qty_step": 0.001, "min_qty": 0.001, "tick_size": 0.1}
        else:
            return {"qty_step": 0.1, "min_qty": 1.0, "tick_size": 0.01}

    def round_step_size(self, quantity, step_size):
        if step_size <= 0:
            return quantity
        precision = int(round(-math.log10(step_size), 0))
        precision = max(0, precision)
        return round(math.floor(quantity / step_size) * step_size, precision)

    def calculate_qty(self, price):
        if price <= 0:
            return 0.0

        specs = self.get_market_precision()
        raw_qty = self.position_size_usdt / price
        final_qty = self.round_step_size(raw_qty, specs["qty_step"])
        
        if final_qty < specs["min_qty"]:
            final_qty = specs["min_qty"]

        return float(final_qty)

    def force_set_leverage(self):
        try:
            self.session.set_leverage(
                category=self.category,
                symbol=self.symbol,
                buyLeverage=str(self.leverage),
                sellLeverage=str(self.leverage)
            )
        except Exception:
            pass

    def get_klines(self, limit=120):
        try:
            res = self.session.get_kline(category=self.category, symbol=self.symbol, interval=self.timeframe, limit=limit)
            raw_list = res.get('result', {}).get('list', [])
            if not raw_list: return pd.DataFrame()

            df = pd.DataFrame(raw_list, columns=['startTime', 'openPrice', 'highPrice', 'lowPrice', 'closePrice', 'volume', 'turnover'])
            df = df.iloc[::-1].reset_index(drop=True)
            for col in ['openPrice', 'highPrice', 'lowPrice', 'closePrice']:
                df[col] = df[col].astype(float)
            return df
        except Exception:
            return pd.DataFrame()

    def update_position_and_pnl(self):
        try:
            res = self.session.get_positions(category=self.category, symbol=self.symbol)
            if res.get("retCode") == 0:
                positions = res.get('result', {}).get('list', [])
                
                found_active = False
                for pos in positions:
                    size = float(pos.get('size', 0))
                    if size > 0:
                        self.pos_side = pos.get('side', "НЕТ")
                        self.pos_size = size
                        self.entry_price = float(pos.get('avgPrice', 0.0))
                        self.sl_price = float(pos.get('stopLoss', 0.0))
                        
                        val = pos.get('positionValue', '')
                        if val and float(val) > 0:
                            self.pos_value_usdt = float(val)
                        else:
                            self.pos_value_usdt = size * self.current_price
                        found_active = True
                        break
                
                if not found_active:
                    self.pos_side = "НЕТ"
                    self.pos_size = 0.0
                    self.pos_value_usdt = 0.0
                    self.entry_price = 0.0
                    self.sl_price = 0.0
                    self.tp_price = 0.0
                    self.breakeven_triggered = False

            today_start_ms = int(datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
            closed_pnl_res = self.session.get_closed_pnl(category=self.category, symbol=self.symbol, startTime=today_start_ms)
            if closed_pnl_res.get("retCode") == 0:
                pnl_list = closed_pnl_res.get('result', {}).get('list', [])
                self.daily_pnl = sum(float(item.get('closedPnl', 0.0)) for item in pnl_list)
        except Exception:
            pass

    def check_dynamic_trailing_stop_alligator(self):
        if self.pos_size <= 0 or self.entry_price <= 0:
            return

        if self.klines_df.empty or len(self.klines_df) < 35:
            return

        try:
            jaw, teeth, lips = calculate_alligator(self.klines_df)
            last_teeth = teeth.dropna().iloc[-1] if not teeth.dropna().empty else None

            if last_teeth is None or math.isnan(last_teeth):
                return

            specs = self.get_market_precision()
            tick_size = specs["tick_size"]
            prec = max(0, int(round(-math.log10(tick_size), 0))) if tick_size > 0 else 2

            new_sl_price = 0.0

            if self.pos_side == "Buy":
                if last_teeth > self.entry_price:
                    if self.sl_price == 0 or last_teeth > self.sl_price:
                        new_sl_price = round(last_teeth, prec)

            elif self.pos_side == "Sell":
                if last_teeth < self.entry_price:
                    if self.sl_price == 0 or last_teeth < self.sl_price:
                        new_sl_price = round(last_teeth, prec)

            if new_sl_price > 0 and new_sl_price != self.sl_price:
                sl_res = self.session.set_trading_stop(
                    category=self.category, 
                    symbol=self.symbol, 
                    stopLoss=str(new_sl_price), 
                    slTriggerBy="LastPrice",
                    positionIdx=0
                )

                if sl_res.get("retCode") == 0:
                    self.sl_price = new_sl_price
                    self.log(f"🐊 Аллигатор (Зубы): Трейлинг SL обновлен до {new_sl_price}")

        except Exception as e:
            self.log(f"Ошибка в динамическом трейлинге Аллигатора: {e}")

    def manual_close_position(self):
        if self.pos_size <= 0:
            self.log("Нет открытой позиции для закрытия!")
            return

        try:
            close_side = "Sell" if self.pos_side == "Buy" else "Buy"
            res = self.session.place_order(
                category=self.category,
                symbol=self.symbol,
                side=close_side,
                orderType="Market",
                qty=str(self.pos_size),
                reduceOnly=True,
                positionIdx=0
            )
            if res.get("retCode") == 0:
                self.log(f"Позиция {self.symbol} успешно закрыта вручную!")
                self.breakeven_triggered = False
                self.update_position_and_pnl()
            else:
                self.log(f"Ошибка закрытия позиции: {res.get('retMsg')}")
        except Exception as e:
            self.log(f"Исключение закрытия позиции: {e}")

    def execute_trade(self, side, price):
        if self.is_executing:
            return
            
        self.is_executing = True
        try:
            self.force_set_leverage()

            qty = self.calculate_qty(price)
            if qty <= 0:
                self.log("Ошибка расчета объема! Отмена ордера.")
                self.is_executing = False
                return
            
            order_res = self.session.place_order(
                category=self.category, 
                symbol=self.symbol, 
                side=side, 
                orderType="Market", 
                qty=str(qty),
                isLeverage=0,
                positionIdx=0
            )
            
            if order_res.get("retCode") != 0:
                self.log(f"Ошибка ордера Bybit [{order_res.get('retCode')}]: {order_res.get('retMsg')}")
                self.is_executing = False
                return

            time.sleep(0.5)

            specs = self.get_market_precision()
            tick_size = specs["tick_size"]
            prec = max(0, int(round(-math.log10(tick_size), 0))) if tick_size > 0 else 2

            if side == "Buy":
                sl_price = round(price * (1 - self.air_bag_pct / 100), prec)
            else:
                sl_price = round(price * (1 + self.air_bag_pct / 100), prec)

            sl_res = self.session.set_trading_stop(
                category=self.category, 
                symbol=self.symbol, 
                stopLoss=str(sl_price), 
                slTriggerBy="LastPrice",
                positionIdx=0
            )

            if sl_res.get("retCode") == 0:
                self.breakeven_triggered = False
                self.log(f"Ордер ИСПОЛНЕН! {side} ({qty} {self.symbol}). Нач. SL: {sl_price}")
            else:
                self.log(f"Ордер {side} открыт, но SL не установлен: {sl_res.get('retMsg')}")

            self.update_position_and_pnl()
        except Exception as e:
            self.log(f"Исключение при исполнении сделки: {e}")
        finally:
            self.is_executing = False

    def stop(self):
        self.running = False

    def run(self):
        last_check_time = 0

        while self.running:
            now = time.time()
            if now - last_check_time >= 2:
                self.update_position_and_pnl()
                last_check_time = now

            df = self.get_klines()
            if not df.empty and len(df) >= 35:
                self.klines_df = df
                self.current_price = df['closePrice'].iloc[-1]

                if self.pos_size > 0:
                    self.check_dynamic_trailing_stop_alligator()

                df['up_fractal'] = (df['highPrice'].shift(2) > df['highPrice'].shift(4)) & \
                                   (df['highPrice'].shift(2) > df['highPrice'].shift(3)) & \
                                   (df['highPrice'].shift(2) > df['highPrice'].shift(1)) & \
                                   (df['highPrice'].shift(2) > df['highPrice'])

                df['down_fractal'] = (df['lowPrice'].shift(2) < df['lowPrice'].shift(4)) & \
                                     (df['lowPrice'].shift(2) < df['lowPrice'].shift(3)) & \
                                     (df['lowPrice'].shift(2) < df['lowPrice'].shift(1)) & \
                                     (df['lowPrice'].shift(2) < df['lowPrice'])

                up_fractals = df[df['up_fractal']]['highPrice']
                down_fractals = df[df['down_fractal']]['lowPrice']

                if self.trading_active:
                    self.status = "Торгует (Поиск)"

                    ema5 = df['closePrice'].ewm(span=5, adjust=False).mean()
                    ema34 = df['closePrice'].ewm(span=34, adjust=False).mean()
                    macd_line = ema5 - ema34
                    macd_hist = macd_line - macd_line.ewm(span=5, adjust=False).mean()

                    last_close = df['closePrice'].iloc[-2]
                    macd_g = macd_hist.iloc[-2] > macd_hist.iloc[-3]
                    macd_r = macd_hist.iloc[-2] < macd_hist.iloc[-3]

                    if not up_fractals.empty and not down_fractals.empty:
                        latest_up_level = up_fractals.iloc[-1]
                        latest_down_level = down_fractals.iloc[-1]

                        if self.pos_size == 0 and not self.is_executing:
                            if (last_close > latest_up_level) and macd_g:
                                self.execute_trade("Buy", self.current_price)
                            elif (last_close < latest_down_level) and macd_r:
                                self.execute_trade("Sell", self.current_price)
                else:
                    self.status = "Пауза (Мониторинг)"

            time.sleep(1)


class AndroidChaosBotApp(App):
    def build(self):
        self.api_key = ""
        self.api_secret = ""
        self.active_bots = {}
        self.symbols_list = ["AVAXUSDT", "XRPUSDT", "1000PEPEUSDT", "ADAUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"]
        self.selected_chart_symbol = None
        self.last_log_date = None

        self.main_layout = BoxLayout(orientation='vertical')

        # Top Bar
        top_bar = BoxLayout(orientation='horizontal', size_hint_y=0.1)
        self.balance_lbl = Label(text="Баланс Demo: --- USD", size_hint_x=0.6)
        api_btn = Button(text="🔑 API", size_hint_x=0.4, on_press=self.open_api_popup)
        top_bar.add_widget(self.balance_lbl)
        top_bar.add_widget(api_btn)
        self.main_layout.add_widget(top_bar)

        # Coin Add Layout
        add_layout = BoxLayout(orientation='horizontal', size_hint_y=0.08)
        self.spinner = Spinner(text=self.symbols_list[0], values=self.symbols_list, size_hint_x=0.6)
        add_btn = Button(text="+ Добавить", size_hint_x=0.4, on_press=self.add_coin_ui)
        add_layout.add_widget(self.spinner)
        add_layout.add_widget(add_btn)
        self.main_layout.add_widget(add_layout)

        # Tabs for Mobile UI
        self.tabs = TabbedPanel(do_default_tab=False, size_hint_y=0.82)
        
        # Tab 1: Coins List
        self.tab_coins = TabbedPanelItem(text="Монеты")
        self.coins_grid = GridLayout(cols=1, spacing=10, size_hint_y=None)
        self.coins_grid.bind(minimum_height=self.coins_grid.setter('height'))
        scroll_coins = ScrollView()
        scroll_coins.add_widget(self.coins_grid)
        self.tab_coins.add_widget(scroll_coins)
        self.tabs.add_widget(self.tab_coins)

        # Tab 2: Chart
        self.tab_chart = TabbedPanelItem(text="График")
        self.chart_container = BoxLayout(orientation='vertical')
        self.fig = Figure(figsize=(4, 4), dpi=100)
        self.fig.patch.set_facecolor('#1E1E1E')
        self.ax = self.fig.add_subplot(111)
        self.canvas_widget = FigureCanvasKivyAgg(self.fig)
        self.chart_container.add_widget(self.canvas_widget)
        self.tab_chart.add_widget(self.chart_container)
        self.tabs.add_widget(self.tab_chart)

        # Tab 3: Logs
        self.tab_logs = TabbedPanelItem(text="Логи")
        self.log_label = Label(text="", size_hint_y=None, halign='left', valign='top')
        self.log_label.bind(texture_size=self.log_label.setter('size'))
        scroll_logs = ScrollView()
        scroll_logs.add_widget(self.log_label)
        self.tab_logs.add_widget(scroll_logs)
        self.tabs.add_widget(self.tab_logs)

        self.main_layout.add_widget(self.tabs)

        self.load_config()
        Clock.schedule_interval(self.update_dashboard, 1.0)
        return self.main_layout

    def log(self, text, raw=False):
        if raw:
            self.log_label.text += text
        else:
            ts = time.strftime("%H:%M:%S")
            self.log_label.text += f"\n[{ts}] {text}"

    def open_api_popup(self, instance):
        content = BoxLayout(orientation='vertical', spacing=10)
        key_input = TextInput(text=self.api_key, hint_text="API Key", multiline=False)
        sec_input = TextInput(text=self.api_secret, hint_text="API Secret", password=True, multiline=False)
        save_btn = Button(text="Сохранить")
        
        content.add_widget(key_input)
        content.add_widget(sec_input)
        content.add_widget(save_btn)

        popup = Popup(title="API Настройки", content=content, size_hint=(0.8, 0.4))
        
        def save_keys(btn_inst):
            self.api_key = key_input.text.strip()
            self.api_secret = sec_input.text.strip()
            for bot in self.active_bots.values():
                bot.update_keys(self.api_key, self.api_secret)
            self.save_config()
            popup.dismiss()

        save_btn.bind(on_press=save_keys)
        popup.open()

    def add_coin_ui(self, instance):
        symbol = self.spinner.text
        if symbol in self.active_bots:
            return
        
        row = BoxLayout(orientation='horizontal', size_hint_y=None, height=40)
        btn_sym = Button(text=symbol, size_hint_x=0.3, on_press=lambda inst: self.select_chart_coin(symbol))
        lbl_info = Label(text="Запуск...", size_hint_x=0.4)
        btn_action = Button(text="Торговать", size_hint_x=0.3)

        def toggle_trade(inst):
            bot = self.active_bots[symbol]
            bot.trading_active = not bot.trading_active
            btn_action.text = "Пауза" if bot.trading_active else "Торговать"

        btn_action.bind(on_press=toggle_trade)
        row.add_widget(btn_sym)
        row.add_widget(lbl_info)
        row.add_widget(btn_action)
        
        self.coins_grid.add_widget(row)

        bot = TradingBotThread(symbol, self.api_key, self.api_secret, self.log)
        self.active_bots[symbol] = bot
        bot._ui_lbl = lbl_info
        bot.start()

        self.select_chart_coin(symbol)
        self.save_config()

    def select_chart_coin(self, symbol):
        self.selected_chart_symbol = symbol
        self.tabs.switch_to(self.tab_chart)

    def draw_chart(self):
        if not self.selected_chart_symbol or self.selected_chart_symbol not in self.active_bots:
            return
        
        bot = self.active_bots[self.selected_chart_symbol]
        if bot.klines_df.empty or len(bot.klines_df) < 35:
            return

        self.ax.clear()
        df = bot.klines_df.tail(40).reset_index(drop=True)
        
        self.ax.plot(df.index, df['closePrice'], color='#7AA2F7', label='Цена')
        jaw, teeth, lips = calculate_alligator(bot.klines_df)
        
        self.ax.plot(jaw.tail(40).reset_index(drop=True), color='#2979FF')
        self.ax.plot(teeth.tail(40).reset_index(drop=True), color='#FF5252')
        self.ax.plot(lips.tail(40).reset_index(drop=True), color='#00E676')

        self.canvas_widget.draw()

    def update_dashboard(self, dt):
        for symbol, bot in self.active_bots.items():
            if hasattr(bot, '_ui_lbl'):
                bot._ui_lbl.text = f"{bot.current_price:.2f} | {bot.pos_side}"
        self.draw_chart()

    def save_config(self):
        data = {
            "api_key": self.api_key,
            "api_secret": self.api_secret,
            "symbols": list(self.active_bots.keys())
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.api_key = data.get("api_key", "")
                self.api_secret = data.get("api_secret", "")
            except Exception:
                pass


if __name__ == '__main__':
    AndroidChaosBotApp().run()