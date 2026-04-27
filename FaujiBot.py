#import MetaTrader5 as mt5
import sys

if sys.platform.startswith('linux'):
    # import mt5linux as MetaTrader5
    from mt5linux import MetaTrader5 as mt5
else:
    import MetaTrader5 as mt5

import time
from datetime import datetime
import json
import argparse
import os
import pandas as pd
import pandas_ta as ta
# from win32con import FALSE
import numpy as np

default_config = {
    "bot_type": "FaujiBot",
    "symbol": "XAUUSDm",
    # "magic_number": 121222,
    "hedge_file_code": "ASP-ADEEL-D",
    "max_allowed_drawdown_percent": 100.0,

    #"magic_number": 123456789,
    "lock_magic_number": True,

    "check_interval_seconds": 0.1,

    "grid_trailing_drop_percent": 30,
    'net_profit_target_usd': 5,
    "max_allowed_grids": 1,  # <-- ADD THIS LINE: Max number of hedged grids allowed before the bot pauses new entries.

}

COMMENT_LENGTH = 31
# ==============================================================================
# HELPER FUNCTIONS (UNCHANGED, THEY WORK)
# ==============================================================================
def _close_single_position_helper(bot_instance, symbol, magic_number, ticket, comment):
    close_attempts = 100
    for i in range(close_attempts):
        pos_tuple = mt5.positions_get(ticket=ticket)
        if not pos_tuple: return False
        position = pos_tuple[0];
        tick = mt5.symbol_info_tick(symbol)
        if not tick: return False
        close_price, close_order_type = (tick.bid, mt5.ORDER_TYPE_SELL) if position.type == mt5.ORDER_TYPE_BUY else (
        tick.ask, mt5.ORDER_TYPE_BUY)
        comment = f"{position.comment}-{comment}"

        # deviation = bot_utils.get_allowed_slippage_points(bot_instance.settings['symbol'])

        if len(comment) > 31: comment = comment[:COMMENT_LENGTH]
        request = {"action": mt5.TRADE_ACTION_DEAL, "position": position.ticket, "symbol": symbol,
                   # "deviation": deviation,
                   "volume": position.volume, "type": close_order_type, "price": close_price, "magic": magic_number,
                   "comment": comment, "type_filling": mt5.ORDER_FILLING_IOC}
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            bot_instance._log(
                f"✅ Individual Position Closed: {position.comment} (Ticket {ticket})")
            return True
        else:
            bot_instance._log(
                f"❌ Individual Position Close Failed (Ticket {ticket})")
            # return False

def check_last_three_signals(my_list, string1):
    """
    Checks if the last item in the list matches string2
    and the second-to-last item matches string1.
    """
    # Ensure the list has at least 2 items
    if len(my_list) >= 3:
        last_item = my_list[-1]  # Access the last item
        second_last_item = my_list[-2]  # Access the second-to-last item
        third_last_item = my_list[-2]  # Access the second-to-last item

        if second_last_item == string1 and last_item == string1 and third_last_item == string1:
            return True
        else:
            return False
    else:
        # Handle cases where the list is too short
        # print("List does not have enough items for the comparison.")
        return False

class MartingaleBot:
    def __init__(self, settings):
        # if settings.get('mt5_port', False):
        #     from mt5linux import MetaTrader5
        #     mt5 = MetaTrader5(host="localhost", port=settings['mt5_port'])

        self.settings = settings
        self.is_running = False

        self.initial_equity, self.bot_peak_equity = 0.0, 0.0

        self.trailing_state_all = False
        self.trailing_state_peak = 0.0

        self.hedge_state = {"baskets": []}

        self.trailing_state_target = 0
        self.hedge_filename = f"bot-{self.settings['symbol']}-{self.settings['magic_number']}-{self.settings.get('hedge_file_code', "1234")}-hedges.json"
        # self._load_hedges()
        self.pnl_is_blue = False

        self.last_processed_broader_trend = datetime.min
        self.market_broader_trend = False

        self.last_processed_market_behavior = datetime.min
        self.market_behavior = ["ANALYZING", 0, "NONE"]
        self.market_behavior2 = False
        self.market_state = "ANALYZING"
        self.market_condition = "ANALYZING"

        self.bot_paused_until = datetime.min
        self.l2_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0, 'trade_type': None}
        self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0, 'trade_type': None}

        self.last_sl_tp_update_level = 0
        self.number_of_positions_while_sl = 0
        self.market_state_history = "Analyzing"
        self.bypass_l3_mtg_first_attempt = False
        self.panic_score = 0
        self.panic_score_requirement = 2
        self.group_trail_states = {}
        self.recovery_trail_states = {}
        self.recovery_grid = {'L3': {'level': 0, 'allowed_levels': 100}, 'L4': {'level': 0, 'allowed_levels': 100}, }
        self.l2_opposite_arming = 0
        self.l2_curfew_sp_price = 0
        self.current_equity_target = 0.0
        self.volatility_event_monitor = {
            'window_start_time': None,    # The Unix timestamp when the 90s window started.
            'peak_in_window': 0.0,        # Highest price seen in the current window.
            'trough_in_window': 0.0,      # Lowest price seen in the current window.
            'is_armed': False,            # Becomes True once a $5 spike is detected.
            'spike_base': 0.0,            # The price where the spike started.
            'spike_tip': 0.0,             # The price at the peak of the spike.
            'spike_direction': 'NONE'     # 'UP' or 'DOWN'
        }
        self.last_processed_bar_time_suggestion2 = datetime.min
        self.suggestion2 = {}
        self.bbb_threshold = []
        self.signal_threshold = []
        self.new_signal = 0

    def _check_for_volatility_event(self):
        """
        A universal volatility detector. It monitors for a price spike of $5 or more
        followed by a 90% retracement, all happening within a rolling 90-second window.
        This function is independent of any open trades.

        Returns:
            bool: True if the specific volatility event is detected, False otherwise.
        """
        tick = mt5.symbol_info_tick(self.settings['symbol'])
        if not tick or tick.time == 0: return False

        current_time = tick.time

        if isinstance(current_time, int):
            current_time = pd.to_datetime(current_time, unit='s')
        else:
            current_time = pd.to_datetime(current_time)

        state = self.volatility_event_monitor

        # --- Window Management ---
        if state['window_start_time'] is None:
            state['window_start_time'] = current_time
            # --- FIX: Initialize with ask and bid ---
            state['peak_in_window'] = tick.ask
            state['trough_in_window'] = tick.bid
            return False

        if (current_time - state['window_start_time']).total_seconds() > 90:
            state.update(window_start_time=current_time, peak_in_window=tick.ask,
                         trough_in_window=tick.bid, is_armed=False)
            return False

        # --- Main Logic within the Active Window ---
        original_peak = state['peak_in_window']
        # --- FIX: Update peak with ask and trough with bid ---
        state['peak_in_window'] = max(state['peak_in_window'], tick.ask)
        state['trough_in_window'] = min(state['trough_in_window'], tick.bid)

        # --- PHASE 1: Spike Detection (if not already armed) ---
        if not state['is_armed']:
            current_range = state['peak_in_window'] - state['trough_in_window']
            if current_range >= 3.0:
                self._log(f"   Volatility Monitor: ARMED. A ${current_range:.2f} spike was detected.")
                state['is_armed'] = True
                if state['peak_in_window'] > original_peak:
                    state['spike_direction'] = 'UP'
                    state['spike_base'] = state['trough_in_window']
                    state['spike_tip'] = state['peak_in_window']
                else:
                    state['spike_direction'] = 'DOWN'
                    state['spike_base'] = state['peak_in_window']
                    state['spike_tip'] = state['trough_in_window']

        # --- PHASE 2: Retracement Monitoring (if armed) ---
        if state['is_armed']:
            total_spike_range = abs(state['spike_tip'] - state['spike_base'])

            # This logic remains correct: if spike was UP, we check the BID for the retrace.
            current_price = tick.bid if state['spike_direction'] == 'UP' else tick.ask
            retrace_amount = abs(state['spike_tip'] - current_price)

            if total_spike_range > 0 and (retrace_amount / total_spike_range) >= 0.90:
                self._log(
                    f"   🚨 VOLATILITY EVENT CONFIRMED! A ${total_spike_range:.2f} spike was followed by a 90%+ retracement.")

                state.update(window_start_time=None, is_armed=False)

                return True

        return False

    def _manage_negative_recovery_trailing(self, target_comments=[], recovery_needed=100.0, trade_level='L3'):
        """
        1. Captures the starting PnL of the group.
        2. Activates only when PnL recovers by 'recovery_needed' amount (e.g. +$100).
        3. Trails the Peak in negatives.
        4. Prints if PnL drops 30% from that Peak.
        """
        settings = self.settings
        symbol, magic = settings['symbol'], settings['magic_number']

        # 1. Get Hedged Tickets
        hedged_tickets = self._get_hedged_tickets()

        # 2. Filter active positions (Partial match logic)
        all_positions = self._get_bot_positions(symbol, magic)

        group_positions = [
            p for p in all_positions
            if p.ticket not in hedged_tickets
        ]

        if not group_positions: return

        # 3. Calculate Current PnL
        current_pnl = sum(p.profit for p in group_positions)

        # 4. State Management
        group_key = tuple(sorted(target_comments))

        if not hasattr(self, 'recovery_trail_states'):
            self.recovery_trail_states = {}

        # INITIALIZE STATE ONLY IF NEW
        # We capture 'initial_pnl' here effectively taking a snapshot of the PnL
        # the very first time this code runs for this group.
        if group_key not in self.recovery_trail_states:
            self.recovery_trail_states[group_key] = {
                'initial_pnl': current_pnl,  # e.g., -500
                'activated': False,
                'peak_pnl': -float('inf')
            }

        state = self.recovery_trail_states[group_key]

        last_trade = [
            p for p in all_positions
            if p.ticket not in hedged_tickets and f"E-{trade_level}-R-{self.recovery_grid[trade_level]['level']} Recovery" in p.comment
        ]
        if last_trade:
            last_trade = last_trade[0]
            price_gap_from_l4 = abs(mt5.symbol_info_tick(self.settings['symbol']).bid - last_trade.price_open)
            if price_gap_from_l4 > 4 and last_trade.profit < 0:
                net_volume = sum(p.volume if p.type == mt5.ORDER_TYPE_BUY else -p.volume for p in group_positions)
                lot_to_neutralize = abs(net_volume)
                final_lot_size = round(lot_to_neutralize, 2)
                recovery_direction = mt5.ORDER_TYPE_BUY if net_volume < 0 else mt5.ORDER_TYPE_SELL
                if self._open_market_order(final_lot_size, f"E-{trade_level}-R-{self.recovery_grid[trade_level]['level']}-D Recovery", recovery_direction,
                                           return_ticket=True):
                    # Reset state so it can start over or simply delete to stop tracking
                    del self.recovery_trail_states[group_key]

        # 5. Activation Logic
        if not state['activated']:
            # Example: Initial (-500) + Recovery Needed (100) = Target (-400)
            activation_target = state['initial_pnl'] + recovery_needed

            if current_pnl >= activation_target:
                state['activated'] = True
                state['peak_pnl'] = current_pnl
                self._log(
                    f"   ⚓ RECOVERY TRAIL ACTIVATED. Base: ${state['initial_pnl']:.2f} -> Recovered ${recovery_needed} -> Current: ${current_pnl:.2f}")
            return

            # 6. Update Peak (Trailing Upwards / Closer to Zero)
        if current_pnl > state['peak_pnl']:
            state['peak_pnl'] = current_pnl

        # 7. Calculate 30% Drop Threshold
        # Logic: 30% of the ABSOLUTE value of the peak.
        # Ex: Peak is -300. Abs is 300. 30% is 90.
        # Threshold = -300 - 90 = -390.
        trail_distance = abs(state['peak_pnl'] - state['initial_pnl'])
        drop_percent = 0.50
        # if trail_distance > 1000:
        #     drop_percent = 0.20
        # elif trail_distance > 500:
        #     drop_percent = 0.30

        allowable_drop = trail_distance * drop_percent
        trigger_level = state['peak_pnl'] - allowable_drop
        reset_now = False
        if abs(state['initial_pnl']) > 500 and abs(current_pnl) < abs(state['initial_pnl'] * 50 / 100):
            reset_now = True

        # 8. Check Trigger
        # add and trigger current_pnl > state['initial_pnl']
        if current_pnl < trigger_level and current_pnl > state['initial_pnl'] or reset_now:
            self._log(
                f"   ⚠️ RECOVERY TRAIL DROP! Peak: ${state['peak_pnl']:.2f} -> Current: ${current_pnl:.2f}. (Threshold: ${trigger_level:.2f})")

            net_volume = sum(p.volume if p.type == mt5.ORDER_TYPE_BUY else -p.volume for p in group_positions)
            lot_to_neutralize = abs(net_volume)
            final_lot_size = round(lot_to_neutralize, 2)
            recovery_direction = mt5.ORDER_TYPE_BUY if net_volume < 0 else mt5.ORDER_TYPE_SELL

            # Territorial Reset
            # if abs(current_pnl) < 150:
            #     l2_positions = self._get_positions_created_after_comment("E-L2")
            #     if not l2_positions:
            #         pass
            #     else:
            #         self._close_specific_positions(f"PC-L2", [p.ticket for p in l2_positions])
            #         self._reset_trailing_grid()
            # elif abs(current_pnl) < 400:
            #     l3_positions = self._get_positions_created_after_comment("E-GN-2") or self._get_positions_created_after_comment("E-L2")
            #     if not l3_positions:
            #         pass
            #     else:
            #         self._close_specific_positions(f"PC-L3", [p.ticket for p in l3_positions])
            #         self._reset_trailing_grid()
            # else:
            #     if self._open_market_order(final_lot_size, f"E-{trade_level}-R-{self.recovery_grid[trade_level]['level']}-D Recovery", recovery_direction, return_ticket=True):
            #         # Reset state so it can start over or simply delete to stop tracking
            #         del self.recovery_trail_states[group_key]

            if self._open_market_order(final_lot_size, f"E-{trade_level}-R-{self.recovery_grid[trade_level]['level']}-D Recovery", recovery_direction, return_ticket=True):
                # Reset state so it can start over or simply delete to stop tracking
                del self.recovery_trail_states[group_key]

    def _get_positions_created_after_comment(self, target_comment="E-L2"):
        symbol, magic = self.settings['symbol'], self.settings['magic_number']

        # 1. Get all positions and SORT them by time (oldest to newest)
        # Sorting is crucial to ensure the timeline is correct
        all_positions = sorted(
            self._get_bot_positions(symbol, magic),
            key=lambda p: p.time
        )

        # 2. Find the Reference Trade (The specific "E-L2" trade)
        # We search for the *last* occurrence of E-L2 in case there are multiple
        l2_trade = None
        for p in all_positions:
            if target_comment in p.comment:
                l2_trade = p
                # We don't break here because we want the LATEST L2 if multiple exist

        # If L2 doesn't exist, we return an empty list
        if not l2_trade:
            return []

        # 3. Filter: Get everything with a time NEWER than L2
        # We use time_msc (milliseconds) for high precision
        newer_positions = [
            p for p in all_positions
            if p.time > l2_trade.time
        ]

        return newer_positions

    def _manage_group_trade_trailing(self, target_comments=[], lock=False):
        """
        Trails the COMBINED profit of trades matching the provided COMMENTS.
        Closes ALL of them if the combined profit drops 30% from its peak.
        """
        if not target_comments: return

        settings = self.settings
        symbol, magic = settings['symbol'], settings['magic_number']

        # 1. Get Hedged Tickets to ignore them
        hedged_tickets = self._get_hedged_tickets()

        # 2. Filter active positions based on COMMENT match
        # We look for positions where the comment is inside the provided list
        target_set = set(target_comments)
        all_positions = self._get_bot_positions(symbol, magic)

        group_positions = [
            p for p in all_positions
            if p.ticket not in hedged_tickets
               # Logic: Check if ANY target string exists inside p.comment
               and any(t in p.comment for t in target_comments)
        ]

        # If no positions found (already closed?), exit
        if not group_positions: return

        if lock and len(target_comments) != len(group_positions): return

        # 3. Calculate COMBINED Profit
        current_group_profit = sum(p.profit for p in group_positions)

        # 4. Manage State
        # We use the sorted list of comments as the unique ID for this group's memory
        group_key = tuple(sorted(target_comments))

        if not hasattr(self, 'group_trail_states'):
            self.group_trail_states = {}

        state = self.group_trail_states.setdefault(group_key, {
            'activated': False,
            'peak_profit': 0.0
        })

        # 5. Activation Logic
        # Use a default setting or hardcoded 1.0 USD threshold
        activation_threshold = settings.get('group_trail_activation_usd', 10.0)

        if not state['activated']:
            if current_group_profit >= activation_threshold:
                state['activated'] = True
                state['peak_profit'] = current_group_profit
                self._log(
                    f"   🔗 GROUP TRAIL ACTIVATED for comments {target_comments}. Total Profit: ${current_group_profit:.2f}")
            return  # Exit if not yet activated

        # 6. Update Peak
        if current_group_profit > state['peak_profit']:
            state['peak_profit'] = current_group_profit

        # 7. Check for Drop (30%)
        drop_percentage = 30.0
        drop_amount = state['peak_profit'] * (drop_percentage / 100.0)
        close_threshold = state['peak_profit'] - drop_amount

        # 8. Execute Close
        if current_group_profit <= close_threshold:
            self._log(
                f"   🔻 GROUP TRAIL HIT for {target_comments}. Peak: ${state['peak_profit']:.2f} -> Current: ${current_group_profit:.2f}. Closing.")

            # We need to get the TICKETS of these positions to close them
            tickets_to_close = [p.ticket for p in group_positions]

            self._close_specific_positions("GT", tickets_to_close)

            # Clean up state
            if group_key in self.group_trail_states:
                del self.group_trail_states[group_key]

    def get_candle_direction(self, symbol, timeframe, num_candles=1):

        # Retrieve the last specified number of candles
        # Using copy_rates_from_pos to get the *last* candle (position 0, count 1)
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, 2)

        if rates is None or len(rates) == 0:
            print("No data retrieved")
            return None

        # Convert the data to a pandas DataFrame for easy analysis
        rates_frame = pd.DataFrame(rates)
        rates_frame['time'] = pd.to_datetime(rates_frame['time'], unit='s')

        # The last row in the DataFrame is the most recent complete candle
        last_candle = rates_frame.iloc[-2]

        # Determine the direction
        if last_candle['close'] > last_candle['open']:
            direction = "Bullish (Up)"
        elif last_candle['close'] < last_candle['open']:
            direction = "Bearish (Down)"
        else:
            direction = "Doji/Neutral (Open equals close)"

    # BOT MAIN FUNCTIONS #
    def _get_market_behavior(self):
        """
        Analyzes the last completed 5-minute candle to determine market behavior.
        Returns:
            tuple: (string state, float range, string direction)
        """

        current_tick_time = mt5.symbol_info_tick(self.settings['symbol']).time
        current_tick_timestamp = pd.to_datetime(current_tick_time, unit='s')
        current_bar_start_time = current_tick_timestamp.floor('1min')

        previous_market_behaviour = self.market_behavior

        if current_bar_start_time <= self.last_processed_market_behavior:
            return self.market_behavior or ["ANALYZING", 0, "NONE"]
        self.last_processed_market_behavior = current_bar_start_time

        rates = mt5.copy_rates_from_pos(self.settings['symbol'], mt5.TIMEFRAME_M5, 0, 2)
        if rates is None or len(rates) < 2:
            return self.market_behavior or ["ANALYZING", 0, "NONE"]

        last_candle = rates[-1]
        price_range = last_candle['open'] - last_candle['low']
        direction = "UP" if last_candle['close'] > last_candle['open'] else "DOWN"

        if price_range >= 4.0:
            # if self.panic_score >= self.panic_score_requirement:
            self.market_behavior = [f"PANIC_{direction}TREND", price_range, direction]
            # self.panic_score += 1

        elif 2.5 <= price_range < 4.0:
            self.panic_score = 0
            self.market_behavior = [f"{direction}TREND", price_range, direction]
        elif price_range <= 2.5:
            self.panic_score = 0
            self.market_behavior = ["SIDEWAYS", price_range, direction]
        else:
            self.panic_score = 0
            self.market_behavior = ["UNCLEAR", price_range, direction]

        second_last_candle = rates[-2]
        # Determine the direction
        if second_last_candle['close'] > second_last_candle['open']:
            self.market_behavior2 = "UP"
        elif last_candle['close'] < last_candle['open']:
            self.market_behavior2 = "DOWN"
        else:
            self.market_behavior2 = False


        current_market_behavior, current_range, current_market_direction = self.market_behavior
        previous_market_behavior, previous_range, previous_market_direction = previous_market_behaviour


        if current_market_behavior != previous_market_behavior or current_market_direction != previous_market_direction:
            self._log(f"   ✅ Behaviour Update: M5 Trend={current_market_behavior}, Range={round(float(current_range), 2)} Direction={current_market_direction}")


    def m15_indicator(self,engine_prefix='e1'):
        # --- 1. Get M15 Settings ---
        timeframe_str = 'M15'
        atr_period = self.settings.get(f'atr_period_{engine_prefix}', 14)
        bb_period = self.settings.get(f'bb_period_{engine_prefix}', 20)
        bb_std_dev = self.settings.get(f'bb_std_dev_{engine_prefix}', 2.0)
        bbb_compression_threshold = self.settings.get(f'bbb_compression_threshold_{engine_prefix}', 0.5)

        # New Momentum Reversal Setting
        reversal_atr_multiplier = self.settings.get(f'reversal_atr_multiplier_{engine_prefix}',
                                                    0.7)  # Body must be > 70% of ATR

        # --- 2. Fetch M15 Data ---
        timeframe_map = {'M15': mt5.TIMEFRAME_M15}
        mt5_timeframe = timeframe_map.get(timeframe_str)

        bars_needed = bb_period * 2
        rates = mt5.copy_rates_from_pos(self.settings['symbol'], mt5_timeframe, 0, bars_needed)
        if rates is None or len(rates) < bars_needed:
            self._log("Not enough M15 data.");
            return

        df = pd.DataFrame(rates)
        current_price = df['close'].iloc[-1]

        # --- 3. Calculate M15 Indicators ---
        # atr_series = ta.atr(df['high'], df['low'], df['close'], length=atr_period)
        # current_atr = atr_series.iloc[-1]

        # --- 3. Calculate M15 Indicators (Manual & Deterministic) ---
        # 1. Calculate Average True Range (ATR)
        # Manual ATR is safer to ensure consistency:
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close = (df['low'] - df['close'].shift()).abs()
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = ranges.max(axis=1)
        # Using Simple Moving Average of TR for stability (matches standard ATR in many contexts)
        # Or use ewm for Wilder's smoothing if preferred. Let's stick to pandas_ta default approximation:
        atr_series = true_range.rolling(window=atr_period).mean()
        # Note: If you specifically need Wilder's RMA, use: true_range.ewm(alpha=1/atr_period, min_periods=atr_period).mean()

        current_atr = atr_series.iloc[-1]
        noise_buffer = current_atr * 0.20

        # Buffer for the Middle Line (Trend Definition)
        # Price must move 10% of ATR away from the center to declare a trend
        trend_buffer = current_atr * 0.30

        # 2. Calculate Bollinger Bands (Manual)
        # Force ddof=0 (Population) or ddof=1 (Sample) to be consistent everywhere.
        # Standard financial tools often use ddof=0 for BB, but Pandas defaults to 1.
        # We will force ddof=0 (Population) to match your Windows result (tighter bands).

        mid = df['close'].rolling(window=bb_period).mean()
        std = df['close'].rolling(window=bb_period).std(ddof=1)  # <--- THE CRITICAL FIX

        upper_series = mid + (std * bb_std_dev)
        lower_series = mid - (std * bb_std_dev)

        # Calculate Bandwidth (BBB)
        # Formula: (Upper - Lower) / Middle
        bbb_series = ((upper_series - lower_series) / mid) * 100

        # Get the final values
        upper_band = upper_series.iloc[-1]
        lower_band = lower_series.iloc[-1]
        bbb = bbb_series.iloc[-1]

        # Safety check for NaNs (start of data)
        if np.isnan(upper_band) or np.isnan(current_atr):
            return

        # --- 4. Determine Baseline Market State ---
        market_trend = 'N/A'

        # Must break UPPER band + buffer
        if current_price > (upper_band + noise_buffer):
            market_trend = 'PANIC_UPTREND'

        # Must break LOWER band - buffer
        elif current_price < (lower_band - noise_buffer):
            market_trend = 'PANIC_DOWNTREND'


        elif bbb < bbb_compression_threshold:
            market_trend = 'SIDEWAYS'
        else:
            mid_point = (upper_band + lower_band) / 2

            # --- NEW: NEUTRAL ZONE LOGIC ---
            if current_price > (mid_point + trend_buffer):
                market_trend = 'UPTREND'
            elif current_price < (mid_point - trend_buffer):
                market_trend = 'DOWNTREND'
            else:
                # Price is inside the "Dead Zone" (Midpoint +/- Buffer)
                # It is too close to the average to determine a strong direction.
                market_trend = 'SIDEWAYS'

        # elif bbb < bbb_compression_threshold:
        #     market_trend = 'SIDEWAYS'
        # else:
        #     mid_point = (upper_band + lower_band) / 2
        #     if current_price > mid_point:
        #         market_trend = 'UPTREND'
        #     else:
        #         market_trend = 'DOWNTREND'

        # --- 5. INSTANT MOMENTUM REVERSAL TRIGGER (NEW LOGIC) ---
        # Get the last two completed candles
        last_candle = df.iloc[-1]
        price_range = abs(last_candle['high'] - last_candle['low'])
        previous_candle = df.iloc[-2]

        # Check for a bearish reversal signal
        if market_trend in ['UPTREND', 'PANIC_UPTREND']:
            is_bearish_candle = last_candle['close'] < last_candle['open']
            candle_body_size = last_candle['open'] - last_candle['close']
            is_strong_body = candle_body_size > (current_atr * reversal_atr_multiplier)
            closed_below_midpoint = last_candle['close'] < (previous_candle['high'] + previous_candle['low']) / 2

            if is_bearish_candle and is_strong_body and closed_below_midpoint and current_price < upper_band:
                self._log(f"   ⚠️ Momentum Override: Strong bearish candle detected. UPTREND -> SIDEWAYS")
                market_trend = 'SIDEWAYS'  # OVERRIDE!

        # Check for a bullish reversal signal
        elif market_trend in ['DOWNTREND', 'PANIC_DOWNTREND']:
            is_bullish_candle = last_candle['close'] > last_candle['open']
            candle_body_size = last_candle['close'] - last_candle['open']
            is_strong_body = candle_body_size > (current_atr * reversal_atr_multiplier)
            closed_above_midpoint = last_candle['close'] > (previous_candle['high'] + previous_candle['low']) / 2

            if is_bullish_candle and is_strong_body and closed_above_midpoint and current_price > lower_band:
                self._log(f"   ⚠️ Momentum Override: Strong bullish candle detected. DOWNTREND -> SIDEWAYS")
                market_trend = 'SIDEWAYS'  # OVERRIDE!

        # --- 6. Calculate Final Distance ---
        base_atr_multiplier = self.settings.get(f'grid_atr_multiplier_{engine_prefix}', 1.2)
        min_safe_distance = self.settings.get(f'min_safe_distance_{engine_prefix}', 3.0)
        max_safe_distance = self.settings.get(f'max_safe_distance_{engine_prefix}', 6.0)

        adjusted_atr_multiplier = base_atr_multiplier
        suggested_distance = current_atr * adjusted_atr_multiplier
        final_distance = np.clip(suggested_distance, min_safe_distance, max_safe_distance)

        if self.suggestion2:
            if self.suggestion2['market_trend'] != market_trend:
                self._log(f"   ✅ Suggestion Update: M15 Trend={market_trend}, ATR={round(float(current_atr), 2)}")
        else:
            self._log(
                f"   ✅ Inital Suggestion: M15 Trend={market_trend}, ATR={round(float(current_atr), 2)}")

        bbb = round(float(bbb), 3)
        real_bbb = bbb
        if bbb > 0.6:
            self.bbb_threshold.append(bbb)
            if not len(self.bbb_threshold) > 3:
                bbb = self.suggestion2['bbb'] if self.suggestion2 else bbb
        else:
            self.bbb_threshold = []

        self.signal_threshold.append(market_trend)
        if market_trend == 'SIDEWAYS':
            self.new_signal = 1
        # if "TREND" in market_trend and check_last_three_signals(self.signal_threshold, market_trend):
        if self.new_signal == 1 and "TREND" in market_trend:
            self.new_signal = 2

        # --- 7. Formulate Suggestion ---
        self.suggestion2 = {
            'price_range': round(float(price_range), 2),
            'market_trend': market_trend,
            'atr_value': round(float(current_atr), 2),
            'final_distance': round(final_distance, 2),
            'bb_upper': round(float(upper_band), 3),
            'bb_lower': round(float(lower_band), 3),
            'bbb': bbb,
            'real_bbb': real_bbb,
            'bbb_range': round(float(abs(upper_band - lower_band)), 3),
            'previous_candle_data': [previous_candle['open'], previous_candle['close']]
        }

    def _calculate_atr_grid_distance(self, engine_prefix='e1', refresh=False):
        """
        M15 Volatility Model + Instant Momentum Reversal Trigger. FINAL VERSION.
        """
        # 1. Get Tick Snapshot ONCE.
        tick = mt5.symbol_info_tick(self.settings['symbol'])
        if tick is None: return

        current_tick_time = mt5.symbol_info_tick(self.settings['symbol']).time
        current_tick_timestamp = pd.to_datetime(current_tick_time, unit='s')
        current_bar_start_time = current_tick_timestamp.floor('5min')

        if current_bar_start_time <= self.last_processed_bar_time_suggestion2 and not refresh:
            return
        self.last_processed_bar_time_suggestion2 = current_bar_start_time

        # if current_bar_start_time >= pd.to_datetime("2025-06-17 06:44:00") and current_bar_start_time <= pd.to_datetime("2025-06-17 06:44:00"):
        #     print("Hook")

        self.m15_indicator()

    def _get_broader_trend(self):
        """
        Determines the broader market trend by analyzing the net direction of the
        last two completed 15-minute candles.
        """
        current_tick_time = mt5.symbol_info_tick(self.settings['symbol']).time
        current_tick_timestamp = pd.to_datetime(current_tick_time, unit='s')
        # Use a 15-minute floor to ensure this only runs once per new M15 candle
        current_bar_start_time = current_tick_timestamp.floor('15min')

        if current_bar_start_time <= self.last_processed_broader_trend:
            return self.market_broader_trend or False

        self.last_processed_broader_trend = current_bar_start_time

        # Fetch 3 candles to ensure we have 2 complete ones to analyze
        rates = mt5.copy_rates_from_pos(self.settings['symbol'], mt5.TIMEFRAME_M15, 0, 3)
        if rates is None or len(rates) < 3:
            self._log("   ⚠️ Could not fetch enough M15 data for broader trend. Using last known trend.")
            return self.market_broader_trend or False

        # We analyze the last 2 *completed* candles.
        # rates[-3] is the first completed candle, rates[-2] is the second.
        first_candle_open = rates[-4]['open']
        last_candle_close = rates[-3]['close']

        if last_candle_close > first_candle_open:
            self.market_broader_trend = "BUY"
            self._log("   📈 Broader Trend (M15x2): UP")
        else:
            self.market_broader_trend = "SELL"
            self._log("   📉 Broader Trend (M15x2): DOWN")

        return self.market_broader_trend

    def _create_hedge_basket(self, grid_positions, comment="HEDGE"):
        symbol, magic = self.settings['symbol'], self.settings['magic_number']

        if not grid_positions: return

        net_buy_volume = sum(p.volume for p in grid_positions if p.type == mt5.ORDER_TYPE_BUY)
        net_sell_volume = sum(p.volume for p in grid_positions if p.type == mt5.ORDER_TYPE_SELL)
        net_exposure = round(net_buy_volume - net_sell_volume, 2)

        if abs(net_exposure) < 0.1:
            self._log("   Grid is already hedged. No action needed.")
            last_trade = sorted(grid_positions, key=lambda p: p.time)[-1]
            if last_trade:
                hedge_type = mt5.ORDER_TYPE_SELL if net_exposure > 0 else mt5.ORDER_TYPE_BUY
                direction_str = "BUY" if hedge_type == mt5.ORDER_TYPE_BUY else "SELL"

                self._log(f"✅ Wraping current grid to hedge basket: 0 lots (Ticket: {last_trade.ticket})")
                basket = {"basket_id": f"{direction_str}_{int(time.time())}", "direction": direction_str,
                          "balance_at_hedge_time": mt5.account_info().balance,
                          "grid_tickets": [p.ticket for p in grid_positions], "hedge_ticket": last_trade.ticket}

                self.hedge_state.setdefault('baskets', []).append(basket)
                self._save_hedges()
                self._log(f"   Hedge basket {basket['basket_id']} created and saved for manual management.")
                return
        else:
            hedge_type = mt5.ORDER_TYPE_SELL if net_exposure > 0 else mt5.ORDER_TYPE_BUY
            hedge_volume = abs(net_exposure)
            price = mt5.symbol_info_tick(symbol).bid if hedge_type == mt5.ORDER_TYPE_SELL else mt5.symbol_info_tick(
                symbol).ask
            direction_str = "BUY" if hedge_type == mt5.ORDER_TYPE_BUY else "SELL"

            if len(comment) > 31: hedge_comment = comment[:COMMENT_LENGTH]
            request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": hedge_volume, "type": hedge_type,
                       "price": price, "magic": magic, "comment": comment, "type_filling": mt5.ORDER_FILLING_IOC}

            result = mt5.order_send(request)

            if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
                self._log(f"❌ FAILED to open hedge trade! Error: {result.comment if result else 'Send Failed'}");
                return

            hedge_ticket = result.order
            self._log(f"✅ 1:1 Hedge placed: {comment} {hedge_volume:.2f} lots (Ticket: {hedge_ticket})")
            basket = {"basket_id": f"{direction_str}_{int(time.time())}", "direction": direction_str,
                      "balance_at_hedge_time": mt5.account_info().balance,
                      "grid_tickets": [p.ticket for p in grid_positions], "hedge_ticket": hedge_ticket}

            self.hedge_state.setdefault('baskets', []).append(basket)
            self._save_hedges()
            self._log(f"   Hedge basket {basket['basket_id']} created and saved for manual management.")

    def _hedge_entire_grid(self, positions, comment="HEDGE"):
        """Calculates net volume of provided positions and places a single opposing trade."""
        if not positions: return

        net_buy_volume = sum(p.volume for p in positions if p.type == mt5.ORDER_TYPE_BUY)
        net_sell_volume = sum(p.volume for p in positions if p.type == mt5.ORDER_TYPE_SELL)
        net_exposure = round(net_buy_volume - net_sell_volume, 2)

        if abs(net_exposure) < 0.1:
            self._log("   Grid is already hedged. No action needed.")
            return

        hedge_type = mt5.ORDER_TYPE_SELL if net_exposure > 0 else mt5.ORDER_TYPE_BUY
        hedge_volume = abs(net_exposure)

        self._log(f"🔥 Placing full grid hedge: {comment} ({hedge_volume} lots).")
        self._open_market_order(hedge_volume, comment, hedge_type)

    def _manage_l1_entry(self):
        if self.pnl_is_blue:
            return
        """Places the L1 trade if no other trades exist."""
        self._log("Grid is empty. Looking to place L1.")
        # trade_direction = self._get_broader_trend()
        market_behavior, _, market_direction = self.market_behavior
        if not market_direction:
            return
        direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
        # direction = mt5.ORDER_TYPE_BUY if trade_direction == "BUY" else mt5.ORDER_TYPE_SELL
        self._open_market_order(0.1, f"E-L1 {market_direction}", direction)

    def _manage_l2_logic(self, positions, market_behavior, market_direction):
        """
        Manages the L2 entry using a trailing activation mechanism.
        It waits for price to cross a gap threshold ($1, $2, or $3), trails the
        peak of that move, and enters on a 30% retracement.
        """
        if self.pnl_is_blue:
            return
        # l1_trade = positions[0]
        l1_trade = [p for p in positions if 'E-L1' in p.comment]
        l2_general = [p for p in positions if 'E-GN-2' in p.comment]

        l1_trade = l1_trade[0] if l1_trade else []
        l2_general = l2_general[0] if l2_general else []
        # Trade Delay Setup
        curfew = False
        tick = mt5.symbol_info_tick(self.settings['symbol'])
        if not tick or tick.time == 0: return False
        current_time = tick.time
        if isinstance(current_time, int):
            current_time = pd.to_datetime(current_time, unit='s')
        else:
            current_time = pd.to_datetime(current_time)

        current_trade = sorted(positions, key=lambda p: p.time)[-1]

        if isinstance(current_trade.time, int):
            current_trade_time = pd.to_datetime(current_trade.time, unit='s')
        else:
            current_trade_time = pd.to_datetime(current_trade.time)

        # Determine the current price that moves AGAINST L1
        current_price = tick.bid if l1_trade.type == mt5.ORDER_TYPE_BUY else tick.ask
        price_gap = abs(current_price - l1_trade.price_open)

        if self.volatility_event_monitor['is_armed'] and not curfew:
            curfew = True
            self.bot_paused_until = current_time + pd.Timedelta(minutes=2)

        if current_trade and (current_time - current_trade_time).total_seconds() < 90 and price_gap >= 4.0:
            curfew = True
            self.bot_paused_until = current_time + pd.Timedelta(minutes=2)

        state = self.l2_trail_state
        # Pause Check using the tick's timestamp
        if (price_gap >= 10.0 and not curfew and current_time > self.bot_paused_until) and not l2_general:
            opposite_type = mt5.ORDER_TYPE_SELL if l1_trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            lot_size = 0.1  # Default for other volumes
            self._log(f"   🔄 L2 General {lot_size} lot opposite direction to L1. Neautralize.")
            self._open_market_order(lot_size, "E-GN-2", opposite_type)
            self.l2_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0, 'trade_type': None}
            return

        # if price_gap >= 5.0:
        #     self.l2_opposite_arming = 1
        #     if self.l2_opposite_arming > 1 and curfew and current_trade_time < self.bot_paused_until:
        #         opposite_type = mt5.ORDER_TYPE_SELL if l1_trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        #         lot_size = 0.5  # Default for other volumes
        #         self._log(f"   🔄 L2 5 Gap Trigger: Opening {lot_size} lot opposite direction to L1.")
        #         self._open_market_order(lot_size, "E-L2 $5-OPP", opposite_type)
        #         self.l2_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0, 'trade_type': None}
        #         return
        # else:
        #     if self.l2_opposite_arming == 1 or self.l2_opposite_arming > 1:
        #         if price_gap <= 4.8:
        #             self.l2_opposite_arming += 1

        market_behavior, _, market_direction = self.market_behavior
        if market_behavior == 'SIDEWAYS':
            opp_gap = 8.0
        else:
            opp_gap = 5.0

        if price_gap >= opp_gap:
            if not (curfew or current_time < self.bot_paused_until):
                if self.l2_curfew_sp_price > 0 and abs(self.l2_curfew_sp_price - current_price) > 5.0 or self.l2_curfew_sp_price == 0:
                    opposite_type = mt5.ORDER_TYPE_SELL if l1_trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                    lot_size = 0.5  # Default for other volumes
                    self._log(f"   🔄 L2 5 Gap Trigger: Opening {lot_size} lot opposite direction to L1.")
                    self._open_market_order(lot_size, "E-L2 $5-OPP", opposite_type)
                    self.l2_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0, 'trade_type': None}
                    return
            else:
                self.l2_curfew_sp_price = current_price
        else:
            self.l2_curfew_sp_price = 0

        if l2_general:
            if "PANIC" in market_behavior:
                pnl = sum(p.profit for p in positions)
                if pnl < 0:
                    symbol_info = mt5.symbol_info(self.settings['symbol'])
                    if not symbol_info or symbol_info.point == 0 or symbol_info.trade_tick_value == 0:
                        self._log("   L3 Condition: ⚠️ Cannot calculate lot size due to invalid symbol info. Skipping.")
                        return

                    # 1. Define how much PnL we need to make.
                    pnl_needed = abs(pnl) + 5.0  # Target a $5 profit buffer

                    # 2. Calculate how much profit a 1.0 lot trade would make in the desired $1.5 price move.
                    # This is the key universal calculation.
                    desired_price_move = 2.0
                    ticks_in_move = desired_price_move / symbol_info.point
                    profit_per_one_lot = ticks_in_move * symbol_info.trade_tick_value

                    if profit_per_one_lot <= 0:
                        self._log("   L2 Condition: ⚠️ Calculated profit per lot is zero or negative. Cannot proceed.")
                        return

                    # 3. The lot size for profit is PnL needed divided by the profit per lot.
                    lot_for_profit = round(pnl_needed / profit_per_one_lot, 2)
                    lot_for_profit = max(lot_for_profit, 0.01)

                    # 4. Calculate the lot size needed to neutralize the current exposure.
                    net_volume = sum(p.volume if p.type == mt5.ORDER_TYPE_BUY else -p.volume for p in positions)
                    lot_to_neutralize = abs(net_volume)

                    # 5. The final lot size is the sum of both parts.
                    final_lot_size = round(lot_to_neutralize + lot_for_profit, 2)
                    # # 1. Calculate the existing net exposure that needs to be neutralized
                    # net_volume = sum(p.volume if p.type == mt5.ORDER_TYPE_BUY else -p.volume for p in positions)
                    # lot_to_neutralize = abs(net_volume)
                    #
                    # # 2. Calculate the additional lot size needed to generate profit
                    # symbol_info = mt5.symbol_info(self.settings['symbol'])
                    # pnl_needed = abs(pnl) + 5.0  # Target a $5 profit buffer
                    # lot_for_profit = round(pnl_needed / (1.5 * symbol_info.trade_contract_size), 2)
                    # lot_for_profit = max(lot_for_profit, 0.01)  # Ensure a minimum
                    #
                    # # 3. The final lot size is the sum of both parts
                    # final_lot_size = round(lot_to_neutralize + lot_for_profit, 2)

                    # 4. Determine direction and execute
                    market_behavior, _, market_direction = self.market_behavior
                    recovery_direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
                    self._log(f"   l2 Condition: PANIC Triggered. Placing recovery trade of {final_lot_size} lots.")
                    self._open_market_order(final_lot_size, "E-L2 Recovery", recovery_direction)
            return
        else:
            # --- PART 1: MANAGE AN ALREADY ACTIVE TRAIL ---
            if state['active']:
                # Check if the price has moved to the *next* price gap level. If so, reset and upgrade the trail.
                next_level = state['level'] + 1.0
                if price_gap >= next_level:
                    self._log(
                        f"   L2 Trail Upgraded: Price blew past ${state['level']} level. Resetting trail for ${next_level} level.")
                    state.update({'active': False, 'level': 0, 'peak_price': 0})
                    return  # Reset and allow the activation logic below to take over on the next tick

                # --- Trail the peak price ---
                if (l1_trade.type == mt5.ORDER_TYPE_BUY and current_price < state['peak_price']) or \
                        (l1_trade.type == mt5.ORDER_TYPE_SELL and current_price > state['peak_price']):
                    state['peak_price'] = current_price

                market_behavior, _, market_direction = self.market_behavior

                direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
                if l1_trade.type != direction:
                    drop_percentage = 0.60
                else:
                    drop_percentage = 0.30

                if market_behavior == 'SIDEWAYS':
                    drop_percentage = .50

                # --- Calculate the 30% retracement trigger ---
                if l1_trade.type == mt5.ORDER_TYPE_BUY:  # L1 is BUY, market is moving DOWN
                    # activation_price = l1_trade.price_open - state['level']
                    activation_price = l1_trade.price_open
                    total_move_from_level = activation_price - state['peak_price']

                    reversal_amount = total_move_from_level * drop_percentage
                    trigger_price = state['peak_price'] + reversal_amount

                    if current_price >= trigger_price:
                        self._log(
                            f"   ✅ L2 Trail Triggered (BUY L1): Retracement hit at ${current_price:.5f}. Opening trade.")
                        self._open_market_order(state['lot_size'], f"E-L2 {state['comment']}", state['trade_type'])
                        state.update({'active': False, 'level': 0, 'peak_price': 0})  # Reset after firing
                        self.l2_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
                                               'trade_type': None}

                else:  # L1 is SELL, market is moving UP
                    # activation_price = l1_trade.price_open + state['level']
                    activation_price = l1_trade.price_open
                    total_move_from_level = state['peak_price'] - activation_price

                    reversal_amount = total_move_from_level * drop_percentage
                    trigger_price = state['peak_price'] - reversal_amount

                    if current_price <= trigger_price:
                        self._log(
                            f"   ✅ L2 Trail Triggered (SELL L1): Retracement hit at ${current_price:.5f}. Opening trade.")
                        self._open_market_order(state['lot_size'], f"E-L2 {state['comment']}", state['trade_type'])
                        state.update({'active': False, 'level': 0, 'peak_price': 0})  # Reset after firing
                        self.l2_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
                                               'trade_type': None}

            # --- PART 2: ACTIVATE A NEW TRAIL ---
            else:
                # if current_trade and (current_time - current_trade_time).total_seconds() < 60:
                #     if price_gap >= 3.0:
                #         opposite_type = mt5.ORDER_TYPE_SELL if l1_trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                #
                #         lot_size = 0.3  # Default for other volumes
                #
                #         self._log(f"   🔄 L2 3# Gap Under 60 Seconds Trigger: Opening {lot_size} lot opposite direction to L1.")
                #         self._open_market_order(lot_size, "E-L2 $3-OPP", opposite_type)
                #         self.l2_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
                #                                'trade_type': None}
                #         return

                if curfew: return
                market_behavior, _, market_direction = self.market_behavior
                if market_behavior == 'SIDEWAYS':
                    gap_levels = [(5.0, 0.4), (4.0, 0.3), (3.0, 0.2)]
                else:
                    gap_levels = [(4.0, 0.4), (3.0, 0.3), (2.0, 0.2)]

                for level, lot_size in gap_levels:
                    if price_gap >= level:
                        self._log(f"   📈 L2 Trail Activated: Price crossed the ${level} gap threshold.")

                        # is_opposite_panic = "PANIC" in market_behavior and \
                        #                     ((market_direction == "UP" and l1_trade.type == mt5.ORDER_TYPE_SELL) or \
                        #                      (market_direction == "DOWN" and l1_trade.type == mt5.ORDER_TYPE_BUY))

                        trade_type, comment = (None, "")
                        # if is_opposite_panic:
                        #     trade_type = mt5.ORDER_TYPE_SELL if l1_trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                        #     comment = "UnevenHedge"
                        # else:
                        trade_type = l1_trade.type
                        comment = "MTG"

                        # Set the state for the new trail
                        state.update({
                            'active': True,
                            'level': level,
                            'peak_price': current_price,
                            'lot_size': lot_size,
                            'trade_type': trade_type,
                            'comment': comment
                        })
                        break  # Stop checking once the highest applicable level is found

    def _manage_l3_logic(self, positions, market_behavior, market_direction):
        if self.pnl_is_blue:
            return

        # Trade Delay Setup
        curfew = False
        tick = mt5.symbol_info_tick(self.settings['symbol'])
        if not tick or tick.time == 0: return False
        current_time = tick.time
        if isinstance(current_time, int):
            current_time = pd.to_datetime(current_time, unit='s')
        else:
            current_time = pd.to_datetime(current_time)

        current_trade = sorted(positions, key=lambda p: p.time)[-1]

        if isinstance(current_trade.time, int):
            current_trade_time = pd.to_datetime(current_trade.time, unit='s')
        else:
            current_trade_time = pd.to_datetime(current_trade.time)

        if current_trade and (current_time - current_trade_time).total_seconds() < 60:
            curfew = True

        """Manages the L3 entry based on L2's lot size and market conditions."""
        l1_trade = [p for p in positions if 'E-L1' in p.comment]
        l2_trade = [p for p in positions if 'E-L2' in p.comment]
        l2_10 = [p for p in positions if 'E-L2 $10-OPP' in p.comment]
        # l2_is_mtg = [p for p in positions if 'E-L2 MTG' in p.comment]
        general_trade = [p for p in positions if 'E-GN-3' in p.comment]

        l1_trade = l1_trade[0] if l1_trade else []
        l2_trade = l2_trade[0] if l2_trade else []
        general_trade = general_trade[0] if general_trade else []

        # l1_trade, l2_trade = positions[0], positions[1]
        last_trade = general_trade or l2_trade
        # price_gap_from_l1 = abs(mt5.symbol_info_tick(self.settings['symbol']).bid - l1_trade.price_open)
        price_gap_from_l2 = abs(mt5.symbol_info_tick(self.settings['symbol']).bid - last_trade.price_open)
        tick = mt5.symbol_info_tick(self.settings['symbol'])
        if not tick: return
        current_price = tick.bid if l2_trade.type == mt5.ORDER_TYPE_BUY else tick.ask
        if self.recovery_grid['L3']['level'] == 0:
            if not [p for p in positions if any(t in p.comment for t in ['E-L2 $10-OPP', 'E-L2 $5-OPP'])]:
                self._manage_group_trade_trailing(['E-L2', 'E-GN-3 GENRL'], lock=True)
        else:
            l3_trade = [p for p in positions if f"E-L3-R-{self.recovery_grid['L3']['level']} Recovery" in p.comment]
            l3_trade_r = [p for p in positions if f"E-L3-R-{self.recovery_grid['L3']['level']}-D Recovery" in p.comment]

            if l3_trade and not l3_trade_r:
                self._manage_negative_recovery_trailing([f"E-L3-R-{self.recovery_grid['L3']['level']} Recovery"], recovery_needed = 100.0, trade_level='L3')
                return

        state = self.l3_trail_state

        market_behavior, _, market_direction = self.market_behavior

        if l2_10:
            if "PANIC" in market_behavior:
                # if not ("PANIC" in self.market_state_history and market_behavior != self.market_state_history): return

                market_behavior, _, market_direction = self.market_behavior
                recovery_direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
                self._log(f"   L3 Condition: PANIC Triggered. Placing recovery trade of {0.5} lots.")
                self._open_market_order(0.5, "E-L3 Recovery", recovery_direction)
            return

        if price_gap_from_l2 >= 7.0 and l2_trade.profit < 0 and l2_trade.type != l1_trade.type and not general_trade:
            net_volume = sum(p.volume if p.type == mt5.ORDER_TYPE_BUY else -p.volume for p in positions)
            lot_to_neutralize = abs(net_volume)
            final_lot_size = round(lot_to_neutralize, 2)
            # final_lot_size = round(lot_to_neutralize + 0.1, 2)
            recovery_direction = mt5.ORDER_TYPE_BUY if net_volume < 0 else mt5.ORDER_TYPE_SELL

            # opposite_direction = mt5.ORDER_TYPE_SELL if l2_trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY

            self._log(f"   L3 Opposite Martingale: L2 was {l2_trade.volume}. Neutralize everything and exposing 0.1 opposite direction.")
            self._open_market_order(final_lot_size, "E-GN-3 GENRL", recovery_direction)

        if general_trade and self.new_signal == 2 and self.suggestion2 and "TREND" in self.suggestion2['market_trend'] and self.suggestion2['bbb'] > 0.6:
            pnl = sum(p.profit for p in positions)

            if pnl < 0:
                market_behavior, _, market_direction = self.market_behavior
                recovery_direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
                self._log(f"   L3 Condition: PANIC Triggered. Placing recovery trade of 0.5 lots.")
                next_level = self.recovery_grid['L3']['level'] + 1

                # Delay Timer
                # last_trade = sorted(positions, key=lambda p: p.time)[-1]
                # if next_level > 1 and last_trade:
                #     if isinstance(last_trade.time, int):
                #         last_trade_time = pd.to_datetime(last_trade.time, unit='s')
                #     else:
                #         last_trade_time = pd.to_datetime(last_trade.time)
                #
                #     if last_trade and (current_time - last_trade_time).total_seconds() < 120:
                #         return

                    # if last_trade.profit > 0:
                    #     return

                recovery_direction = mt5.ORDER_TYPE_BUY if self.suggestion2['market_trend'] in ['UPTREND', 'PANIC_UPTREND'] else mt5.ORDER_TYPE_SELL

                if self._open_market_order(0.5, f"E-L3-R-{next_level} Recovery", recovery_direction, return_ticket=True):
                    self.recovery_grid['L3']['level'] += 1

            # if price_gap_from_l2 >= 2.0:
            #     self._log(f"   L3 General Trade Condition: L2 was {l2_trade.volume}. Neutralize everything and exposing 0.1 opposite direction.")
            #     self._open_market_order(0.4, "E-L3 MTG", last_trade.type)

        if self.volatility_event_monitor['is_armed'] and not curfew:
            curfew = True
            self.bot_paused_until = current_time + pd.Timedelta(minutes=2)

        if self.bot_paused_until and current_time < self.bot_paused_until:
            return  # Bot is in a timed pause, do nothing

        if curfew: return

        # --- Case where L2 was a small martingale trade (0.2 lot) ---
        elif l1_trade.type == l2_trade.type and not general_trade and [p for p in positions if 'E-L2 MTG' in p.comment]:
            if state['active']:
                # --- Trail the peak price ---
                if (l2_trade.type == mt5.ORDER_TYPE_BUY and current_price < state['peak_price']) or \
                        (l2_trade.type == mt5.ORDER_TYPE_SELL and current_price > state['peak_price']):
                    state['peak_price'] = current_price
                # --- Calculate the 30% retracement trigger ---
                if l2_trade.type == mt5.ORDER_TYPE_BUY:  # L2 is BUY, market is moving DOWN
                    # activation_price = l1_trade.price_open - state['level']
                    activation_price = l2_trade.price_open
                    total_move_from_level = activation_price - state['peak_price']

                    market_behavior, _, market_direction = self.market_behavior
                    direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
                    if l1_trade.type != direction:
                        reversal_amount = total_move_from_level * 0.60
                    else:
                        reversal_amount = total_move_from_level * 0.50

                    trigger_price = state['peak_price'] + reversal_amount

                    if current_price >= trigger_price:
                        self._log(
                            f"   ✅ L3 Trail Triggered (L2): Retracement hit at ${current_price:.5f}. Opening trade.")
                        self._open_market_order(state['lot_size'], f"E-L3 {state['comment']}A", state['trade_type'])
                        state.update({'active': False, 'level': 0, 'peak_price': 0})  # Reset after firing
                        self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
                                               'trade_type': None}
                else:
                    # activation_price = l1_trade.price_open + state['level']
                    activation_price = l2_trade.price_open
                    total_move_from_level = state['peak_price'] - activation_price

                    market_behavior, _, market_direction = self.market_behavior
                    direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
                    if l1_trade.type != direction:
                        reversal_amount = total_move_from_level * 0.60
                    else:
                        reversal_amount = total_move_from_level * 0.50

                    trigger_price = state['peak_price'] - reversal_amount

                    if current_price <= trigger_price:
                        if not self.bypass_l3_mtg_first_attempt:
                            self.bypass_l3_mtg_first_attempt = True
                            self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
                                                   'trade_type': None}
                            return
                        self._log(
                            f"   ✅ L3 Trail Triggered: Retracement hit at ${current_price:.5f}. Opening trade.")
                        self._open_market_order(state['lot_size'], f"E-L3 {state['comment']}B", state['trade_type'])
                        state.update({'active': False, 'level': 0, 'peak_price': 0})  # Reset after firing
                        self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
                                               'trade_type': None}
            else:
                if price_gap_from_l2 >= 3.0:
                    self._log(f"   📈 L3 Trail Activated: Price crossed the ${price_gap_from_l2} gap threshold.")

                    trade_type = l2_trade.type
                    comment = "MTG"
                    volume_to_trade = 0.4

                    # Set the state for the new trail
                    state.update({
                        'active': True,
                        'peak_price': current_price,
                        'lot_size': volume_to_trade,
                        'trade_type': trade_type,
                        'comment': comment
                    })

            # if price_gap_from_l2 >= 5.0 and not general_trade:
            #     net_volume = sum(p.volume if p.type == mt5.ORDER_TYPE_BUY else -p.volume for p in positions)
            #     lot_to_neutralize = abs(net_volume)
            #     final_lot_size = round(lot_to_neutralize + 0.1, 2)
            #     recovery_direction = mt5.ORDER_TYPE_BUY if net_volume < 0 else mt5.ORDER_TYPE_SELL
            #
            #     # opposite_direction = mt5.ORDER_TYPE_SELL if l2_trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            #
            #     self._log(f"   L3 General Trade Condition: L2 was {l2_trade.volume}. Neutralize everything and exposing 0.1 opposite direction.")
            #     self._open_market_order(final_lot_size, "E-GN-3 GENRL", recovery_direction)
            market_behavior, _, market_direction = self.market_behavior
            if market_behavior == 'SIDEWAYS':
                opp_gap = 10.0
            else:
                opp_gap = 7.0

            if price_gap_from_l2 >= opp_gap:
                opposite_type = mt5.ORDER_TYPE_SELL if l2_trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY

                lot_size = 1.4  # Default for other volumes

                self._log(f"   🔄 L3 8 Gap Trigger: Opening {lot_size} lot opposite direction to L2.")
                self._open_market_order(lot_size, "E-L3 $8-OPP", opposite_type)
                self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0, 'trade_type': None}
                return

            # volume_to_trade = 0.4 if l2_trade.volume == 0.2 else 0.5
            # if price_gap_from_l2 >= 3.0:  # If trend is slow/sideways
            #     self._log("   L3 Condition: Slow trend from 0.2 L2. Continuing martingale with 0.4.")
            #     self._open_market_order(volume_to_trade, "E-L3 Martingale", l1_trade.type)

        # --- Case where L2 was a larger martingale trade (0.3 or 0.4 lot) ---
        elif l1_trade.type != l2_trade.type and not general_trade and [p for p in positions if 'E-L2 $5-OPP' in p.comment]:

            if state['active']:
                # --- Trail the peak price ---
                if (l2_trade.type == mt5.ORDER_TYPE_BUY and current_price < state['peak_price']) or \
                        (l2_trade.type == mt5.ORDER_TYPE_SELL and current_price > state['peak_price']):
                    state['peak_price'] = current_price
                # --- Calculate the 30% retracement trigger ---
                if l2_trade.type == mt5.ORDER_TYPE_BUY:  # L2 is BUY, market is moving DOWN
                    # activation_price = l1_trade.price_open - state['level']
                    activation_price = l2_trade.price_open
                    total_move_from_level = activation_price - state['peak_price']

                    market_behavior, _, market_direction = self.market_behavior
                    direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
                    if l1_trade.type != direction:
                        reversal_amount = total_move_from_level * 0.60
                    else:
                        reversal_amount = total_move_from_level * 0.50

                    trigger_price = state['peak_price'] + reversal_amount

                    if current_price >= trigger_price:
                        self._log(
                            f"   ✅ L3 Trail Triggered (L2): Retracement hit at ${current_price:.5f}. Opening trade.")
                        self._open_market_order(state['lot_size'], f"E-L3 {state['comment']}A", state['trade_type'])
                        state.update({'active': False, 'level': 0, 'peak_price': 0})  # Reset after firing
                        self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
                                               'trade_type': None}
                else:
                    # activation_price = l1_trade.price_open + state['level']
                    activation_price = l2_trade.price_open
                    total_move_from_level = state['peak_price'] - activation_price

                    market_behavior, _, market_direction = self.market_behavior
                    direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
                    if l1_trade.type != direction:
                        reversal_amount = total_move_from_level * 0.60
                    else:
                        reversal_amount = total_move_from_level * 0.50

                    trigger_price = state['peak_price'] - reversal_amount

                    if current_price <= trigger_price:
                        if not self.bypass_l3_mtg_first_attempt:
                            self.bypass_l3_mtg_first_attempt = True
                            self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
                                                   'trade_type': None}
                            return
                        self._log(
                            f"   ✅ L3 Trail Triggered: Retracement hit at ${current_price:.5f}. Opening trade.")
                        self._open_market_order(state['lot_size'], f"E-L3 {state['comment']}B", state['trade_type'])
                        state.update({'active': False, 'level': 0, 'peak_price': 0})  # Reset after firing
                        self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
                                               'trade_type': None}
            else:
                if price_gap_from_l2 >= 3.0 and l2_trade.profit < 0 and not general_trade:
                    self._log(f"   📈 L3 Trail Activated: Price crossed the ${price_gap_from_l2} gap threshold.")

                    trade_type = l2_trade.type
                    comment = "MTG"
                    volume_to_trade = 0.3

                    # Set the state for the new trail
                    state.update({
                        'active': True,
                        'peak_price': current_price,
                        'lot_size': volume_to_trade,
                        'trade_type': trade_type,
                        'comment': comment
                    })

    # def _manage_l3_logic(self, positions, market_behavior, market_direction):
    #     if self.pnl_is_blue:
    #         return
    #
    #     # Trade Delay Setup
    #     curfew = False
    #     tick = mt5.symbol_info_tick(self.settings['symbol'])
    #     if not tick or tick.time == 0: return False
    #     current_time = tick.time
    #     if isinstance(current_time, int):
    #         current_time = pd.to_datetime(current_time, unit='s')
    #     else:
    #         current_time = pd.to_datetime(current_time)
    #
    #     current_trade = sorted(positions, key=lambda p: p.time)[-1]
    #
    #     if isinstance(current_trade.time, int):
    #         current_trade_time = pd.to_datetime(current_trade.time, unit='s')
    #     else:
    #         current_trade_time = pd.to_datetime(current_trade.time)
    #
    #     if current_trade and (current_time - current_trade_time).total_seconds() < 60:
    #         curfew = True
    #
    #     """Manages the L3 entry based on L2's lot size and market conditions."""
    #     l1_trade = [p for p in positions if 'E-L1' in p.comment]
    #     l2_trade = [p for p in positions if 'E-L2' in p.comment]
    #     l2_10 = [p for p in positions if 'E-L2 $10-OPP' in p.comment]
    #
    #     general_trade = [p for p in positions if 'E-GN-3' in p.comment]
    #
    #     l1_trade = l1_trade[0] if l1_trade else []
    #     l2_trade = l2_trade[0] if l2_trade else []
    #     general_trade = general_trade[0] if general_trade else []
    #
    #     # l1_trade, l2_trade = positions[0], positions[1]
    #     last_trade = general_trade or l2_trade
    #     # price_gap_from_l1 = abs(mt5.symbol_info_tick(self.settings['symbol']).bid - l1_trade.price_open)
    #     price_gap_from_l2 = abs(mt5.symbol_info_tick(self.settings['symbol']).bid - last_trade.price_open)
    #     tick = mt5.symbol_info_tick(self.settings['symbol'])
    #     if not tick: return
    #     current_price = tick.bid if l2_trade.type == mt5.ORDER_TYPE_BUY else tick.ask
    #
    #     if not [p for p in positions if any(t in p.comment for t in ['E-L2 $10-OPP', 'E-L2 $5-OPP'])]:
    #         self._manage_group_trade_trailing(['E-L2', 'E-GN-3 GENRL'], lock=True)
    #
    #     state = self.l3_trail_state
    #
    #     market_behavior, _, market_direction = self.market_behavior
    #
    #     if l2_10:
    #         if "PANIC" in market_behavior:
    #             # if not ("PANIC" in self.market_state_history and market_behavior != self.market_state_history): return
    #
    #             market_behavior, _, market_direction = self.market_behavior
    #             recovery_direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
    #             self._log(f"   L3 Condition: PANIC Triggered. Placing recovery trade of {0.5} lots.")
    #             self._open_market_order(0.5, "E-L3 Recovery", recovery_direction)
    #         return
    #
    #     if price_gap_from_l2 >= 5.0 and l2_trade.profit < 0 and not general_trade:
    #         net_volume = sum(p.volume if p.type == mt5.ORDER_TYPE_BUY else -p.volume for p in positions)
    #         lot_to_neutralize = abs(net_volume)
    #         final_lot_size = round(lot_to_neutralize + 0.1, 2)
    #         recovery_direction = mt5.ORDER_TYPE_BUY if net_volume < 0 else mt5.ORDER_TYPE_SELL
    #
    #         # opposite_direction = mt5.ORDER_TYPE_SELL if l2_trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    #
    #         self._log(f"   L3 General Trade Condition: L2 was {l2_trade.volume}. Neutralize everything and exposing 0.1 opposite direction.")
    #         self._open_market_order(final_lot_size, "E-GN-3 GENRL", recovery_direction)
    #
    #     if general_trade and "PANIC" in market_behavior:
    #         # if not ("PANIC" in self.market_state_history and market_behavior != self.market_state_history): return
    #         # if curfew: return
    #         pnl = sum(p.profit for p in positions)
    #         if pnl < 0:
    #             symbol_info = mt5.symbol_info(self.settings['symbol'])
    #             if not symbol_info or symbol_info.point == 0 or symbol_info.trade_tick_value == 0:
    #                 self._log("   L3 Condition: ⚠️ Cannot calculate lot size due to invalid symbol info. Skipping.")
    #                 return
    #
    #             # 1. Define how much PnL we need to make.
    #             pnl_needed = abs(pnl) + 5.0  # Target a $5 profit buffer
    #
    #             # 2. Calculate how much profit a 1.0 lot trade would make in the desired $1.5 price move.
    #             # This is the key universal calculation.
    #             desired_price_move = 1.5
    #             ticks_in_move = desired_price_move / symbol_info.point
    #             profit_per_one_lot = ticks_in_move * symbol_info.trade_tick_value
    #
    #             if profit_per_one_lot <= 0:
    #                 self._log("   L3 Condition: ⚠️ Calculated profit per lot is zero or negative. Cannot proceed.")
    #                 return
    #
    #             # 3. The lot size for profit is PnL needed divided by the profit per lot.
    #             lot_for_profit = round(pnl_needed / profit_per_one_lot, 2)
    #             lot_for_profit = max(lot_for_profit, 0.01)
    #
    #             # 4. Calculate the lot size needed to neutralize the current exposure.
    #             net_volume = sum(p.volume if p.type == mt5.ORDER_TYPE_BUY else -p.volume for p in positions)
    #             lot_to_neutralize = abs(net_volume)
    #
    #             # 5. The final lot size is the sum of both parts.
    #             final_lot_size = round(lot_to_neutralize + lot_for_profit, 2)
    #             # # 1. Calculate the existing net exposure that needs to be neutralized
    #             # net_volume = sum(p.volume if p.type == mt5.ORDER_TYPE_BUY else -p.volume for p in positions)
    #             # lot_to_neutralize = abs(net_volume)
    #             #
    #             # # 2. Calculate the additional lot size needed to generate profit
    #             # symbol_info = mt5.symbol_info(self.settings['symbol'])
    #             # pnl_needed = abs(pnl) + 5.0  # Target a $5 profit buffer
    #             # lot_for_profit = round(pnl_needed / (1.5 * symbol_info.trade_contract_size), 2)
    #             # lot_for_profit = max(lot_for_profit, 0.01)  # Ensure a minimum
    #             #
    #             # # 3. The final lot size is the sum of both parts
    #             # final_lot_size = round(lot_to_neutralize + lot_for_profit, 2)
    #
    #             # 4. Determine direction and execute
    #             market_behavior, _, market_direction = self.market_behavior
    #             recovery_direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
    #             self._log(f"   L4 Condition: PANIC Triggered. Placing recovery trade of {final_lot_size} lots.")
    #             self._open_market_order(final_lot_size, "E-L3 Recovery", recovery_direction)
    #
    #         # if price_gap_from_l2 >= 2.0:
    #         #     self._log(f"   L3 General Trade Condition: L2 was {l2_trade.volume}. Neutralize everything and exposing 0.1 opposite direction.")
    #         #     self._open_market_order(0.4, "E-L3 MTG", last_trade.type)
    #
    #     if self.volatility_event_monitor['is_armed'] and not curfew:
    #         curfew = True
    #         self.bot_paused_until = current_time.floor('2min')
    #
    #     if self.bot_paused_until and current_trade_time < self.bot_paused_until:
    #         return  # Bot is in a timed pause, do nothing
    #
    #     if curfew: return
    #
    #     # --- Case where L2 was a small martingale trade (0.2 lot) ---
    #     elif l1_trade.type == l2_trade.type and not general_trade:
    #         if state['active']:
    #             # --- Trail the peak price ---
    #             if (l2_trade.type == mt5.ORDER_TYPE_BUY and current_price < state['peak_price']) or \
    #                     (l2_trade.type == mt5.ORDER_TYPE_SELL and current_price > state['peak_price']):
    #                 state['peak_price'] = current_price
    #             # --- Calculate the 30% retracement trigger ---
    #             if l2_trade.type == mt5.ORDER_TYPE_BUY:  # L2 is BUY, market is moving DOWN
    #                 # activation_price = l1_trade.price_open - state['level']
    #                 activation_price = l2_trade.price_open
    #                 total_move_from_level = activation_price - state['peak_price']
    #
    #                 market_behavior, _, market_direction = self.market_behavior
    #                 direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
    #                 if l1_trade.type != direction:
    #                     reversal_amount = total_move_from_level * 0.60
    #                 else:
    #                     reversal_amount = total_move_from_level * 0.50
    #
    #                 trigger_price = state['peak_price'] + reversal_amount
    #
    #                 if current_price >= trigger_price:
    #                     self._log(
    #                         f"   ✅ L3 Trail Triggered (L2): Retracement hit at ${current_price:.5f}. Opening trade.")
    #                     self._open_market_order(state['lot_size'], f"E-L3 {state['comment']}A", state['trade_type'])
    #                     state.update({'active': False, 'level': 0, 'peak_price': 0})  # Reset after firing
    #                     self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
    #                                            'trade_type': None}
    #             else:
    #                 # activation_price = l1_trade.price_open + state['level']
    #                 activation_price = l2_trade.price_open
    #                 total_move_from_level = state['peak_price'] - activation_price
    #
    #                 market_behavior, _, market_direction = self.market_behavior
    #                 direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
    #                 if l1_trade.type != direction:
    #                     reversal_amount = total_move_from_level * 0.60
    #                 else:
    #                     reversal_amount = total_move_from_level * 0.50
    #
    #                 trigger_price = state['peak_price'] - reversal_amount
    #
    #                 if current_price <= trigger_price:
    #                     if not self.bypass_l3_mtg_first_attempt:
    #                         self.bypass_l3_mtg_first_attempt = True
    #                         self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
    #                                                'trade_type': None}
    #                         return
    #                     self._log(
    #                         f"   ✅ L3 Trail Triggered: Retracement hit at ${current_price:.5f}. Opening trade.")
    #                     self._open_market_order(state['lot_size'], f"E-L3 {state['comment']}B", state['trade_type'])
    #                     state.update({'active': False, 'level': 0, 'peak_price': 0})  # Reset after firing
    #                     self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
    #                                            'trade_type': None}
    #         else:
    #             if price_gap_from_l2 >= 3.0:
    #                 self._log(f"   📈 L3 Trail Activated: Price crossed the ${price_gap_from_l2} gap threshold.")
    #
    #                 trade_type = l2_trade.type
    #                 comment = "MTG"
    #                 volume_to_trade = 0.4
    #
    #                 # Set the state for the new trail
    #                 state.update({
    #                     'active': True,
    #                     'peak_price': current_price,
    #                     'lot_size': volume_to_trade,
    #                     'trade_type': trade_type,
    #                     'comment': comment
    #                 })
    #
    #         # if price_gap_from_l2 >= 5.0 and not general_trade:
    #         #     net_volume = sum(p.volume if p.type == mt5.ORDER_TYPE_BUY else -p.volume for p in positions)
    #         #     lot_to_neutralize = abs(net_volume)
    #         #     final_lot_size = round(lot_to_neutralize + 0.1, 2)
    #         #     recovery_direction = mt5.ORDER_TYPE_BUY if net_volume < 0 else mt5.ORDER_TYPE_SELL
    #         #
    #         #     # opposite_direction = mt5.ORDER_TYPE_SELL if l2_trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    #         #
    #         #     self._log(f"   L3 General Trade Condition: L2 was {l2_trade.volume}. Neutralize everything and exposing 0.1 opposite direction.")
    #         #     self._open_market_order(final_lot_size, "E-GN-3 GENRL", recovery_direction)
    #
    #         if price_gap_from_l2 >= 7.0:
    #             opposite_type = mt5.ORDER_TYPE_SELL if l2_trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    #
    #             lot_size = 1.4  # Default for other volumes
    #
    #             self._log(f"   🔄 L3 8 Gap Trigger: Opening {lot_size} lot opposite direction to L2.")
    #             self._open_market_order(lot_size, "E-L3 $8-OPP", opposite_type)
    #             self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0, 'trade_type': None}
    #             return
    #
    #         # volume_to_trade = 0.4 if l2_trade.volume == 0.2 else 0.5
    #         # if price_gap_from_l2 >= 3.0:  # If trend is slow/sideways
    #         #     self._log("   L3 Condition: Slow trend from 0.2 L2. Continuing martingale with 0.4.")
    #         #     self._open_market_order(volume_to_trade, "E-L3 Martingale", l1_trade.type)
    #
    #     # --- Case where L2 was a larger martingale trade (0.3 or 0.4 lot) ---
    #     elif l1_trade.type != l2_trade.type and not general_trade:
    #
    #         if state['active']:
    #             # --- Trail the peak price ---
    #             if (l2_trade.type == mt5.ORDER_TYPE_BUY and current_price < state['peak_price']) or \
    #                     (l2_trade.type == mt5.ORDER_TYPE_SELL and current_price > state['peak_price']):
    #                 state['peak_price'] = current_price
    #             # --- Calculate the 30% retracement trigger ---
    #             if l2_trade.type == mt5.ORDER_TYPE_BUY:  # L2 is BUY, market is moving DOWN
    #                 # activation_price = l1_trade.price_open - state['level']
    #                 activation_price = l2_trade.price_open
    #                 total_move_from_level = activation_price - state['peak_price']
    #
    #                 market_behavior, _, market_direction = self.market_behavior
    #                 direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
    #                 if l1_trade.type != direction:
    #                     reversal_amount = total_move_from_level * 0.60
    #                 else:
    #                     reversal_amount = total_move_from_level * 0.50
    #
    #                 trigger_price = state['peak_price'] + reversal_amount
    #
    #                 if current_price >= trigger_price:
    #                     self._log(
    #                         f"   ✅ L3 Trail Triggered (L2): Retracement hit at ${current_price:.5f}. Opening trade.")
    #                     self._open_market_order(state['lot_size'], f"E-L3 {state['comment']}A", state['trade_type'])
    #                     state.update({'active': False, 'level': 0, 'peak_price': 0})  # Reset after firing
    #                     self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
    #                                            'trade_type': None}
    #             else:
    #                 # activation_price = l1_trade.price_open + state['level']
    #                 activation_price = l2_trade.price_open
    #                 total_move_from_level = state['peak_price'] - activation_price
    #
    #                 market_behavior, _, market_direction = self.market_behavior
    #                 direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
    #                 if l1_trade.type != direction:
    #                     reversal_amount = total_move_from_level * 0.60
    #                 else:
    #                     reversal_amount = total_move_from_level * 0.50
    #
    #                 trigger_price = state['peak_price'] - reversal_amount
    #
    #                 if current_price <= trigger_price:
    #                     if not self.bypass_l3_mtg_first_attempt:
    #                         self.bypass_l3_mtg_first_attempt = True
    #                         self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
    #                                                'trade_type': None}
    #                         return
    #                     self._log(
    #                         f"   ✅ L3 Trail Triggered: Retracement hit at ${current_price:.5f}. Opening trade.")
    #                     self._open_market_order(state['lot_size'], f"E-L3 {state['comment']}B", state['trade_type'])
    #                     state.update({'active': False, 'level': 0, 'peak_price': 0})  # Reset after firing
    #                     self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
    #                                            'trade_type': None}
    #         else:
    #             if price_gap_from_l2 >= 3.0 and l2_trade.profit < 0 and not general_trade:
    #                 self._log(f"   📈 L3 Trail Activated: Price crossed the ${price_gap_from_l2} gap threshold.")
    #
    #                 trade_type = l2_trade.type
    #                 comment = "MTG"
    #                 volume_to_trade = 0.3
    #
    #                 # Set the state for the new trail
    #                 state.update({
    #                     'active': True,
    #                     'peak_price': current_price,
    #                     'lot_size': volume_to_trade,
    #                     'trade_type': trade_type,
    #                     'comment': comment
    #                 })

    def _manage_l4_logic(self, positions, market_behavior, current_tick_time):
        """
        Manages the L4 entry with two distinct strategies based on market conditions.
        1. PANIC: Aggressive recovery trade.
        2. NORMAL: A trade to adjust the grid's net exposure to 0.1 in the broader trend's direction.
        """
        if self.pnl_is_blue:
            return

        # Trade Delay Setup
        curfew = False
        tick = mt5.symbol_info_tick(self.settings['symbol'])
        if not tick or tick.time == 0: return False
        current_time = tick.time
        if isinstance(current_time, int):
            current_time = pd.to_datetime(current_time, unit='s')
        else:
            current_time = pd.to_datetime(current_time)

        current_trade = sorted(positions, key=lambda p: p.time)[-1]

        if isinstance(current_trade.time, int):
            current_trade_time = pd.to_datetime(current_trade.time, unit='s')
        else:
            current_trade_time = pd.to_datetime(current_trade.time)

        if current_trade and (current_time - current_trade_time).total_seconds() < 90:
            curfew = True

        l1_trade = [p for p in positions if 'E-L1' in p.comment]
        l2_trade = [p for p in positions if 'E-L2' in p.comment]
        l3_trade = [p for p in positions if 'E-L3' in p.comment]
        l4_general_trade = [p for p in positions if 'E-GN-4' in p.comment]

        l1_trade = l1_trade[0] if l1_trade else []
        l2_trade = l2_trade[0] if l2_trade else []
        l3_trade = l3_trade[0] if l3_trade else []
        l4_general_trade = l4_general_trade[0] if l4_general_trade else []

        if self.recovery_grid['L4']['level'] == 0:
            self._manage_group_trade_trailing(['E-L3', 'E-GN-3', 'E-GN-4'], lock=True)
        else:
            l4_trade = [p for p in positions if f"E-L4-R-{self.recovery_grid['L4']['level']} Recovery" in p.comment]
            l4_trade_r = [p for p in positions if f"E-L4-R-{self.recovery_grid['L4']['level']}-D Recovery" in p.comment]

            if l4_trade and not l4_trade_r:
                self._manage_negative_recovery_trailing([f"E-L4-R-{self.recovery_grid['L4']['level']} Recovery"], recovery_needed = 100.0, trade_level='L4')
                return

        last_trade = l4_general_trade or l3_trade
        # l3_trade = positions[0]
        price_gap_from_l3 = abs(mt5.symbol_info_tick(self.settings['symbol']).bid - last_trade.price_open)

        if (l1_trade.type == l2_trade.type == l3_trade.type) and price_gap_from_l3 >= 6.0 and not l4_general_trade:
            net_volume = sum(p.volume if p.type == mt5.ORDER_TYPE_BUY else -p.volume for p in positions)
            lot_to_neutralize = abs(net_volume)
            final_lot_size = round(lot_to_neutralize, 2)

            # final_lot_size = round(lot_to_neutralize + 0.1, 2)
            self._log(
                f"   L4 General Trade Condition: L3 was {l3_trade.volume}. Neutralize everything and exposing 0.1 opposite direction.")
            recovery_direction = mt5.ORDER_TYPE_BUY if net_volume < 0 else mt5.ORDER_TYPE_SELL
            self._open_market_order(final_lot_size, "E-GN-4 GENRL", recovery_direction)

        # --- SCENARIO A: CORRECTED PANIC RECOVERY STRATEGY ---
        elif l4_general_trade and self.new_signal == 2 and self.market_behavior2 and self.suggestion2 and "TREND" in self.suggestion2['market_trend'] and self.suggestion2['bbb'] > 0.6:
            # if curfew: return
            # if not ("PANIC" in self.market_state_history and market_behavior != self.market_state_history): return
            pnl = sum(p.profit for p in positions)

            if pnl < 0:
                market_behavior, _, market_direction = self.market_behavior
                recovery_direction = mt5.ORDER_TYPE_BUY if market_direction == "UP" else mt5.ORDER_TYPE_SELL
                self._log(f"   L4 Condition: PANIC Triggered. Placing recovery trade of 0.5 lots.")
                next_level = self.recovery_grid['L4']['level'] + 1

                # Delay timer
                # last_trade = sorted(positions, key=lambda p: p.time)[-1]
                # if next_level > 1 and last_trade:
                #     if isinstance(last_trade.time, int):
                #         last_trade_time = pd.to_datetime(last_trade.time, unit='s')
                #     else:
                #         last_trade_time = pd.to_datetime(last_trade.time)
                #
                #     if last_trade and (current_time - last_trade_time).total_seconds() < 120:
                #         return

                    # if last_trade.profit > 0:
                    #     return

                recovery_direction = mt5.ORDER_TYPE_BUY if self.suggestion2['market_trend'] in ['UPTREND', 'PANIC_UPTREND'] else mt5.ORDER_TYPE_SELL

                if self._open_market_order(0.5, f"E-L4-R-{next_level} Recovery", recovery_direction, return_ticket=True):
                    self.recovery_grid['L4']['level'] += 1

        # --- SCENARIO B: NORMAL EXPOSURE CONTROL STRATEGY (Unchanged) ---
        else:
            if price_gap_from_l3 >= 6.0 and not l4_general_trade and l3_trade.profit < 0:
                net_volume = sum(p.volume if p.type == mt5.ORDER_TYPE_BUY else -p.volume for p in positions)
                lot_to_neutralize = abs(net_volume)
                final_lot_size = round(lot_to_neutralize, 2)

                # final_lot_size = round(lot_to_neutralize + 0.1, 2)
                recovery_direction = mt5.ORDER_TYPE_BUY if net_volume < 0 else mt5.ORDER_TYPE_SELL
                # opposite_type = mt5.ORDER_TYPE_SELL if l3_trade.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                # lot_size = l3_trade.volume
                self._log(
                    f"   L4 General Trade Condition: L3 was {l3_trade.volume}. Neutralize everything and exposing 0.1 opposite direction.")
                # self._log(f"   🔄 L4 5 Gap Trigger: Opening {lot_size} neutralizing L3.")
                self._open_market_order(final_lot_size, "E-GN-4 GENRL", recovery_direction)

    def _manage_l5_hedge_logic(self, positions, current_tick_time):
        """
        Monitors an active L4 trade. If the price moves $3 against it,
        this function triggers the full L5 grid hedge and pauses the bot.
        """
        if self.pnl_is_blue:
            return

        # The L4 trade is the most recent one in the sequence of four

        hedged_tickets = self._get_hedged_tickets()
        positions = [p for p in self._get_bot_positions(self.settings['symbol'], self.settings['magic_number'])
                     if p.ticket not in hedged_tickets]
        l4_trade = sorted(positions, key=lambda p: p.time)[-1]
        price_gap_from_l4 = abs(mt5.symbol_info_tick(self.settings['symbol']).bid - l4_trade.price_open)
        pnl = sum(p.profit for p in positions)
        if price_gap_from_l4 >= 3.0 and pnl < 0:
            self._log("   L4 Failure: Trade moved $3 against. Hedging entire grid for L5.")
            self._create_hedge_basket(positions, "L5-GridHedge")

            # Use timedelta for correct backtesting
            # self.bot_paused_until = current_tick_time.floor('1min')
            self._log(
                f"   BOT PAUSED. Will resume after simulated time: {self.bot_paused_until.strftime('%Y-%m-%d %H:%M:%S')}")

    def _load_hedges(self):
        if os.path.exists(self.hedge_filename):
            try:
                with open(self.hedge_filename, 'r') as f:
                    self.hedge_state = json.load(f)
                self._log(
                    f"✅ Loaded {len(self.hedge_state.get('baskets', []))} hedge baskets from {self.hedge_filename}")
            except Exception as e:
                self._log(f"❌ FAILED to load hedge file {self.hedge_filename}: {e}")

    def _save_hedges(self):
        try:
            with open(self.hedge_filename, 'w') as f:
                json.dump(self.hedge_state, f, indent=2)
        except Exception as e:
            self._log(f"❌ FAILED to save hedge file {self.hedge_filename}: {e}")

    def _get_hedged_tickets(self):
        return {ticket for basket in self.hedge_state.get('baskets', []) for ticket in
                basket.get('grid_tickets', []) + [basket.get('hedge_ticket')]}

    def _log(self, message):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")

    def _get_bot_positions(self, symbol, magic_number):
        """
        Gets all positions for a symbol and manually filters them by magic number.
        This is a reliable replacement for mt5.positions_get(symbol, magic).
        """
        all_positions = mt5.positions_get(symbol=symbol)
        if all_positions is None:
            return []  # Return an empty list on error or if no positions exist

        if not self.settings['lock_magic_number']:
            return all_positions

        # Filter positions by magic number
        all_positions = [pos for pos in all_positions if pos.magic == magic_number]
        return all_positions

    def _get_positions_by_tickets(self, ticket_list):
        """A helper to get a list of position objects from a list of tickets."""
        if not ticket_list:
            return []
        all_positions = self._get_bot_positions(self.settings['symbol'], self.settings['magic_number'])
        return [p for p in all_positions if p.ticket in ticket_list]

    def start(self):
        self._log("🚀 Starting Fauji Bot (Gold)...")
        if not mt5.initialize(): self._log(f"❌ MT5 initialization failed."); return
        if not self._check_symbol(): mt5.shutdown(); return

        self.is_running = True
        self.initial_equity, self.bot_peak_equity = mt5.account_info().equity, mt5.account_info().equity
        self._log(f"✅ Bot initialized on {self.settings['symbol']}. Initial Equity: ${self.initial_equity:,.2f}")
        self._load_hedges()
        self._verify_and_clean_hedges()

        return True
        # self.main_loop()

    def stop(self, reason=""):
        self.is_running = False;
        self._log(f"🛑 Shutting down... Reason: {reason}");
        mt5.shutdown();
        self._log("MT5 closed.")

    def _verify_and_clean_hedges(self):
        """Checks if all trades in the hedge file still exist. Removes baskets that are fully closed."""
        all_open_tickets = {p.ticket for p in
                            self._get_bot_positions(self.settings['symbol'], self.settings['magic_number'])}
        baskets_to_remove = [i for i, basket in enumerate(self.hedge_state.get('baskets', [])) if
                             not (set(basket.get('grid_tickets', [])) | {basket.get('hedge_ticket')}).intersection(
                                 all_open_tickets)]
        if baskets_to_remove:
            self._log(f"   🧹 Cleaning hedge file of {len(baskets_to_remove)} fully closed baskets.")
            for i in sorted(baskets_to_remove, reverse=True): del self.hedge_state['baskets'][i]
            self._save_hedges()

    def _check_symbol(self):
        selected = mt5.symbol_select(self.settings['symbol'], True)
        if not selected:
            self._log(
                f"Failed to select {self.settings['symbol']} in Market Watch. Check if the symbol name is correct or available with your broker.")
            return False
        i = mt5.symbol_info(self.settings['symbol']);
        if not i: self._log(f"❌ Symbol '{self.settings['symbol']}' not found."); return False
        if not i.visible and not mt5.symbol_select(self.settings['symbol'], True): self._log(
            f"❌ Failed to enable symbol '{self.settings['symbol']}'."); return False
        return True

    def _delta_watchdog(self, current_equity):
        """
        Implements a compounding profit target. When equity reaches the next
        "step," it closes all positions to realize the gain and sets a new, higher target.
        """
        if not self.settings.get('delta_watchdog_enabled', False):
            return

        # On the very first run, establish the initial target
        if self.current_equity_target == 0.0:
            profit_step = self.settings.get('watchdog_equity_gain_target', 500.0)
            self.current_equity_target = self.initial_equity + profit_step
            self._log(f"   STEP LADDER Initialized. First target: ${self.current_equity_target:,.2f}")

        if self.settings.get('delta_trailing_drop_percent', False):
            # --- PHASE 1: TRAILING LOGIC (IF ACTIVATED) ---
            if self.equity_trail_activated:
                # Update the peak equity seen during this trail
                self.peak_equity_in_trail = max(self.peak_equity_in_trail, current_equity)

                # Calculate the drop threshold
                drop_percent = self.settings.get('delta_trailing_drop_percent', 10.0)
                drop_amount = self.peak_equity_in_trail * (drop_percent / 100.0)
                closing_threshold = self.peak_equity_in_trail - drop_amount

                # Check if the trigger condition is met
                if current_equity <= closing_threshold:
                    self._log(
                        f"✅ STEP LADDER TRAIL HIT! Equity dropped to ${current_equity:.2f} from a peak of ${self.peak_equity_in_trail:.2f}.")
                    self._log("   -> Closing all positions to lock in profit.")

                    self._close_all_positions_and_orders("Step-Ladder-Trail-TP")

                    # Set the new target for the next cycle
                    profit_step = self.settings.get('watchdog_equity_gain_target', 500.0)
                    # The new floor is the target we just surpassed
                    self.current_equity_target += profit_step

                    self._log(f"   -> Profit locked. Next equity target is ${self.current_equity_target:,.2f}.")

                    # Reset the trail for the next cycle
                    self.equity_trail_activated = False
                    self.peak_equity_in_trail = 0.0

                    time.sleep(1)  # Brief pause for order processing
                    self._verify_and_clean_hedges()

                    return  # End the function for this tick

            # --- PHASE 2: ACTIVATION LOGIC (IF NOT ALREADY TRAILING) ---
            elif current_equity >= self.current_equity_target:
                self._log(
                    f"✅ STEP LADDER TARGET REACHED! Equity (${current_equity:.2f}) > Target (${self.current_equity_target:.2f}).")
                self._log(f"   -> EQUITY TRAIL ACTIVATED. Will now trail equity from this point.")

                self.equity_trail_activated = True
                self.peak_equity_in_trail = current_equity
        else:
            # --- THE ONLY TRIGGER CONDITION ---
            if current_equity >= self.current_equity_target:
                self._log(
                    f"✅ STEP LADDER TARGET HIT! Equity (${current_equity:.2f}) reached target (${self.current_equity_target:.2f}).")
                self._log("   -> Closing all positions to lock in profit.")

                # Close everything to realize the gains
                self._close_all_positions_and_orders("Step-Ladder-TP")

                # Set the new, higher target
                profit_step = self.settings.get('watchdog_equity_gain_target', 500.0)
                self.current_equity_target += profit_step

                self._log(f"   -> Profit locked. Next equity target is ${self.current_equity_target:,.2f}.")

                # A brief pause to ensure orders are processed before the next tick.
                # This is a safety measure in a live environment.
                time.sleep(1)


    def tick(self):
        """
        Executes a single cycle of the bot's trading logic.
        This method is NON-BLOCKING and is intended to be called in a loop by a manager process.
        """
        try:
            # NOTE: The 'while self.is_running:' loop has been removed from here.

            # --- All of your existing trading logic from main_loop stays here ---
            account_info = mt5.account_info()
            if not account_info:
                self._log("⚠️ Could not get account info during tick.")
                return  # Exit this tick if we can't get info

            self._delta_watchdog(account_info.equity)
            # self._check_for_volatility_event()
            self._manage_grid_close_all()

            tick_info = mt5.symbol_info_tick(self.settings['symbol'])
            if not tick_info:
                return  # Skip if tick is invalid

            # Get the current time from the MT5 tick first!
            current_tick_time = mt5.symbol_info_tick(self.settings['symbol']).time
            if isinstance(current_tick_time, int):
                current_tick_time = pd.to_datetime(current_tick_time, unit='s')
            else:
                current_tick_time = pd.to_datetime(current_tick_time)

            # Get unhedged positions to determine the current state
            hedged_tickets = self._get_hedged_tickets()
            positions = [p for p in self._get_bot_positions(self.settings['symbol'], self.settings['magic_number'])
                         if p.ticket not in hedged_tickets]
            num_trades = len(positions)

            current_pnl = abs(sum(p.profit for p in positions))

            # Get Market Sensor Data
            self._get_market_behavior()
            self._calculate_atr_grid_distance()
            market_behavior, _, market_direction = self.market_behavior
            self._check_for_volatility_event()

            self.market_state = market_behavior

            if market_behavior == "ANALYZING": return

            l1 = [p for p in positions if 'E-L1' in p.comment]
            l2 = [p for p in positions if 'E-L2' in p.comment]
            l3 = [p for p in positions if 'E-L3' in p.comment]
            if not l3 or l3 and "E-L3-R" in l3[0].comment:
                if self.recovery_grid['L3']['level'] >= self.recovery_grid['L3']['allowed_levels']:
                    l3 = [p for p in positions if f"E-L3-R-{self.recovery_grid['L3']['level']} Recovery" in p.comment]
                else:
                    l3 = []
                l3 = []
            if self.recovery_grid['L4']['level'] >= self.recovery_grid['L4']['allowed_levels']:
                l4 = [p for p in positions if f"E-L4-R-{self.recovery_grid['L4']['level']} Recovery" in p.comment]
            else:
                l4 = []

            # ==============================================================================
            # --- NEW: Grid Limit Enforcement ---
            # ==============================================================================
            # This is the master control for starting new grids.
            if num_trades == 0:
                max_grids_allowed = self.settings.get("max_allowed_grids", 999)
                num_hedged_grids = len(self.hedge_state.get('baskets', []))

                if num_hedged_grids >= max_grids_allowed:
                    # Log this message periodically to avoid spamming the console.
                    # We can use the timestamp for a simple periodic log.
                    if current_tick_time.second % 10 == 0:
                        self._log(
                            f"   INFO: Max hedged grids ({max_grids_allowed}) reached. Waiting for a grid to be resolved before starting a new L1.")
                    return  # VITAL: Exit the function to prevent a new grid from starting.
                else:
                    # Pause Check using the tick's timestamp
                    # if self.bot_paused_until and current_tick_time < self.bot_paused_until:
                    #     return  # Bot is in a timed pause, do nothing
                    self._manage_l1_entry()  # Limit not reached, proceed to create L1.

            # ==============================================================================
            if current_pnl > 1500:
                self._manage_l5_hedge_logic(positions, current_tick_time)
                return
            # State Machine: Call the handler for the current level
            elif l1 and not l2:
                self._manage_l2_logic(positions, market_behavior, market_direction)
            elif l2 and not l3:
                self._manage_l3_logic(positions, market_behavior, market_direction)
            elif l3 and not l4:
                self._manage_l4_logic(positions, market_behavior, current_tick_time)
            elif l4:
                # NEW: Once L4 is placed, we now monitor for L5 hedge conditions
                self._manage_l5_hedge_logic(positions, current_tick_time)

            self.market_state_history = market_behavior
            self.new_signal = 0 if self.new_signal == 2 else self.new_signal
            # L5 is a hedged state, not an entry, so it's handled by the L4 failure condition.

            # --- Manage Closing and Rescue Operations (Run independently) ---
            # NOTE: Your _manage_grid_close_all would go here if you want to close the ACTIVE grid
            # self._manage_grid_close_all()

            # self._manage_rescue_trades() # The L6 logic would be a separate function call here

            # self._verify_and_clean_hedges()
            # --- End of existing logic ---

            # The time.sleep() is also removed from here, as the BotWorker will now handle it.

        except Exception as e:
            # We still keep the try/except here to catch errors within a single tick
            self._log(f"🔥 An unexpected error occurred during a tick: {e}")
            import traceback

            self._log(f"{traceback.format_exc()}")
            # Depending on the error, you might want to stop the bot
            # self.stop(f"Runtime Error: {e}")

    def main_loop(self):
        try:
            while self.is_running:
                self.tick()
                time.sleep(self.settings['check_interval_seconds'])
        except KeyboardInterrupt:
            # self._close_all_pending_orders()
            self.stop("Keyboard Interrupt")
        except Exception as e:
            self._log(f"🔥 An unexpected error occurred: {e}"); self.stop(f"Runtime Error: {e}")

    def get_account_info_snapshot(self):
        symbol, magic = self.settings['symbol'], self.settings['magic_number']
        positions = [p for p in self._get_bot_positions(symbol, magic)]
        pnl = sum(p.profit for p in positions)
        return {
            'balance': mt5.account_info().balance,
            'equity': mt5.account_info().equity,
            'pnl': pnl
            # 'pnl': mt5.account_info().equity - mt5.account_info().balance
        }
    def get_exposure_snapshot(self):
        """
        Calculates and returns the net exposure (total buy/sell volume) and
        the overall breakeven price for all open positions.
        This is part of the common interface for all bot types.
        """
        symbol = self.settings.get('symbol')
        magic = self.settings.get('magic_number')

        if not self.is_running or not symbol or not magic:
            return None

        try:
            positions = self._get_bot_positions(symbol, magic)
            if not positions:
                return None  # Return None if there are no positions

            buy_volume = 0.0
            sell_volume = 0.0

            # This will be the sum of (price * volume * contract_size) for all trades
            total_trade_value = 0.0

            # Get symbol information to find the contract size (e.g., 100 for XAUUSD)
            symbol_info = mt5.symbol_info(symbol)
            if not symbol_info:
                self._log(f"Could not get symbol info for {symbol} to calculate breakeven.")
                return None
            contract_size = symbol_info.trade_contract_size

            # --- Loop 1: Calculate total volumes and total trade value ---
            for pos in positions:
                if pos.type == mt5.ORDER_TYPE_BUY:
                    buy_volume += pos.volume
                    total_trade_value += pos.price_open * pos.volume
                elif pos.type == mt5.ORDER_TYPE_SELL:
                    sell_volume += pos.volume
                    total_trade_value -= pos.price_open * pos.volume

            # Round volumes to handle potential floating point inaccuracies
            buy_volume = round(buy_volume, 2)
            sell_volume = round(sell_volume, 2)

            net_volume = round(buy_volume - sell_volume, 2)

            breakeven_price = 0.0

            # --- Calculate Breakeven Price ---
            # The breakeven can only be calculated if there is a net long or net short position.
            # If net_volume is zero, the position is perfectly hedged, and breakeven is not applicable.
            if net_volume != 0:
                # The formula is: (Sum of all trade values) / (Net Volume * Contract Size)
                # We already have the sum of trade values from the loop.
                breakeven_price = (total_trade_value * contract_size) / (net_volume * contract_size)

                # Simplified: breakeven_price = total_trade_value / net_volume
                # Note: The above simplification is valid if all trades are on the same asset.
                # Your current code uses a slightly different calculation method, so let's use a weighted average:

                total_weighted_price = 0
                for pos in positions:
                    if pos.type == mt5.ORDER_TYPE_BUY:
                        total_weighted_price += pos.price_open * pos.volume
                    else:  # SELL
                        total_weighted_price -= pos.price_open * pos.volume

                if net_volume != 0:
                    breakeven_price = total_weighted_price / net_volume

            # --- Determine Exposure Type and Net Exposed Volume ---
            exposure_type = "HEDGED"
            exposed_volume = 0.0

            if net_volume > 0:
                exposure_type = "BUY"
                exposed_volume = net_volume
            elif net_volume < 0:
                exposure_type = "SELL"
                exposed_volume = abs(net_volume)

            return {
                "buy_volume": buy_volume,
                "sell_volume": sell_volume,
                "exposure_type": exposure_type,
                "exposed_volume": exposed_volume,
                "breakeven_price": breakeven_price
            }

        except Exception as e:
            self._log(f"Error calculating exposure snapshot: {e}")
            return None

    def _reset_trailing_grid(self, soft=False):
        self.trailing_state_peak = 0.0
        self.last_sl_tp_update_level = 0
        self.number_of_positions_while_sl = 0
        self._verify_and_clean_hedges()
        self.pnl_is_blue = False
        self.l2_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
                               'trade_type': None}
        self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
                               'trade_type': None}
        self.trailing_state_all = False
        self.bypass_l3_mtg_first_attempt = False
        if self.trailing_state_all:
            self._log("   ✅ Grid has been closed by SL/TP. Resetting for next cycle.")
        self.l2_opposite_arming = 0
        self.l2_curfew_sp_price = 0
        self.recovery_trail_states = {}
        if not soft:
            self.recovery_grid['L3']['level'] = 0
            self.recovery_grid['L4']['level'] = 0

    def _manage_grid_close_all(self):
        # todo: get rid of this in production and fix sl/tp logic for prodution
        symbol, magic = self.settings['symbol'], self.settings['magic_number']

        positions = [p for p in self._get_bot_positions(symbol, magic) if p.ticket not in self._get_hedged_tickets()]

        floating_pnl = sum(p.profit for p in positions)
        state = self.trailing_state_all

        if floating_pnl > 0:
            self.pnl_is_blue = True
        else:
            self.pnl_is_blue = False

        if len(positions) <= 2:
            net_profit_target = 10.0
        else:
            net_profit_target = len(positions) * self.settings.get('net_profit_target_usd', 1.0)

        # --- TRAILING STOP LOGIC ---
        if state:
            if floating_pnl > self.trailing_state_peak:
                self.trailing_state_peak = floating_pnl

            drop_amount = self.trailing_state_peak * (self.settings['grid_trailing_drop_percent'] / 100.0)

            # CORRECTED TRAILING LOGIC
            # drop_amount = self.trailing_state_peak * (self.settings['grid_trailing_drop_percent'] / 100.0)
            # drop_amount = (self.trailing_state_peak - net_profit_target) * (self.settings['grid_trailing_drop_percent'] / 100.0)
            closing_threshold = self.trailing_state_peak - drop_amount

            if floating_pnl <= closing_threshold:
                self._log(
                    f"🔻TRAILING STOP HIT at P/L ${floating_pnl:.2f} (Peak was ${self.trailing_state_peak:.2f}). Closing active grid.")
                self._close_specific_positions(f"GTS-A", [p.ticket for p in positions])

                self._verify_and_clean_hedges()
                # self._close_all_pending_orders()
                self._reset_trailing_grid()
        else:
            if floating_pnl >= net_profit_target:
                self._log(f"✅ TRAILING ACTIVATED. P/L: ${floating_pnl:.2f}")
                self.trailing_state_all = True
                self.trailing_state_peak = floating_pnl
                self.trailing_state_target = net_profit_target


    # def _manage_grid_close_all(self):
    #     # Todo fix sl/tp in produciton
    #     symbol, magic = self.settings['symbol'], self.settings['magic_number']
    #     positions = [p for p in self._get_bot_positions(symbol, magic) if p.ticket not in self._get_hedged_tickets()]
    #     # positions = self._get_bot_positions(symbol, magic)
    #
    #     # --- RESET and EXIT if grid is closed ---
    #     if not positions:
    #         self._reset_trailing_grid()
    #         self.recovery_grid['L3']['level'] = 0
    #         self.recovery_grid['L4']['level'] = 0
    #         return
    #
    #     floating_pnl = sum(p.profit for p in positions)
    #
    #     if floating_pnl > 0:
    #         self.pnl_is_blue = True
    #     else:
    #         self.pnl_is_blue = False
    #
    #     if len(positions) > 0 and self.number_of_positions_while_sl > 0 and self.number_of_positions_while_sl != len(positions):
    #         self._close_specific_positions(f"GTS-A", [p.ticket for p in positions])
    #         self.recovery_grid['L3']['level'] = 0
    #         self.recovery_grid['L4']['level'] = 0
    #         self._reset_trailing_grid()
    #         return
    #
    #     # --- PROFIT TARGET CALCULATION ---
    #     if len(positions) <= 2:
    #         net_profit_target = 10.0
    #     else:
    #         net_profit_target = len(positions) * self.settings.get('net_profit_target_usd', 1.0)
    #
    #     # (Your unhedge_loss logic can remain here)
    #     # hedge_split = (self.unhedge_loss * self.settings.get('unhedge_loss_recovery_percentage', 5.0) / 100)
    #     # net_profit_target += hedge_split
    #
    #     # --- TRAILING LOGIC ---
    #     if self.trailing_state_all:
    #         if floating_pnl > self.trailing_state_peak:
    #             self.trailing_state_peak = floating_pnl
    #
    #         # Determine current profit multiple (e.g., 1x, 2x, 3x the target)
    #         if self.trailing_state_target > 0:
    #             current_profit_level = int(self.trailing_state_peak / self.trailing_state_target)
    #         else:
    #             current_profit_level = 1
    #
    #         # Only update SL/TP if we have reached a NEW level of profit
    #         if current_profit_level > self.last_sl_tp_update_level:
    #             self._log(
    #                 f"   📈 Profit reached Level {current_profit_level} (Peak PnL: ${self.trailing_state_peak:.2f}).")
    #
    #             # Calculate the new profit to lock in
    #             drop_percent = self.settings['grid_trailing_drop_percent'] / 100.0
    #             profit_to_lock_in = self.trailing_state_peak * (1 - drop_percent)
    #
    #             # Update the SL/TP for all trades to this new, tighter level
    #             if self._update_grid_sl_tp_to_lock_profit(positions, profit_to_lock_in):
    #                 self.number_of_positions_while_sl = len(positions)
    #                 self.last_sl_tp_update_level = current_profit_level  # Record that we've updated for this level
    #             else:
    #                 self._log("   ⚠️ SL/TP update failed. Will retry on next trigger.")
    #
    #             # Todo remove in production.
    #             if self.last_sl_tp_update_level > 10:
    #                 self._close_specific_positions(f"GTS-A", [p.ticket for p in positions])
    #
    #     # --- ACTIVATION LOGIC ---
    #     else:
    #         if floating_pnl >= net_profit_target:
    #             self._log(f"✅ TRAILING ACTIVATED at P/L ${floating_pnl:.2f}. Setting initial SL/TP.")
    #             self.trailing_state_all = True
    #             self.trailing_state_peak = floating_pnl
    #             self.trailing_state_target = net_profit_target
    #
    #             # Immediately set the first SL/TP to lock in profit
    #             drop_percent = self.settings['grid_trailing_drop_percent'] / 100.0
    #             profit_to_lock_in = self.trailing_state_peak * (1 - drop_percent)
    #
    #             if self._update_grid_sl_tp_to_lock_profit(positions, profit_to_lock_in):
    #                 self.number_of_positions_while_sl = len(positions)
    #                 self.last_sl_tp_update_level = 1  # We have set the SL/TP for level 1

    def _update_grid_sl_tp_to_lock_profit(self, positions, target_pnl_lock_in):
        """
        Calculates a unified price point for a target PnL and modifies all positions' SL/TP.
        This version is robust, works for all account types, and respects the broker's "Stops Level"
        to prevent "Invalid stops" errors.
        """
        if not positions: return False

        symbol = self.settings['symbol']
        symbol_info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if not all([symbol_info, tick]) or symbol_info.point == 0 or symbol_info.trade_tick_value == 0:
            self._log(f"❌ Cannot set SL/TP: Symbol info or tick for {symbol} is invalid.")
            return False

        # --- 1. Calculate Grid Breakeven and Net Volume (Unchanged) ---
        net_volume = 0.0
        total_value = 0.0
        for p in positions:
            if p.type == mt5.ORDER_TYPE_BUY:
                net_volume += p.volume
                total_value += p.price_open * p.volume
            elif p.type == mt5.ORDER_TYPE_SELL:
                net_volume -= p.volume
                total_value -= p.price_open * p.volume

        net_volume = round(net_volume, 2)
        if abs(net_volume) < 0.01:
            return False

        breakeven_price = total_value / net_volume

        # --- 2. Calculate Target Price using Universal Formula (Unchanged) ---
        price_change_needed = (target_pnl_lock_in * symbol_info.point) / (symbol_info.trade_tick_value * net_volume)
        target_close_price = breakeven_price + price_change_needed

        self._log(
            f"   🔒 Updating SL/TP. Target PnL: ${target_pnl_lock_in:.2f} -> Calculated Price: {target_close_price:.5f}")

        # --- 3. NEW: Respect Broker's Stops Level ---
        stop_level_distance = symbol_info.trade_stops_level * symbol_info.point

        # Adjust for BUY positions (TP must be above ASK, SL must be below BID)
        if net_volume > 0:  # Grid is net BUY
            # If our target is a Take Profit, ensure it's not too close
            if target_close_price > tick.ask:
                min_tp_price = tick.ask + stop_level_distance
                if target_close_price < min_tp_price:
                    self._log(
                        f"   ⚠️ TP price too close. Adjusting from {target_close_price:.5f} to {min_tp_price:.5f}")
                    target_close_price = min_tp_price
            # If our target is a Stop Loss, ensure it's not too close
            else:
                max_sl_price = tick.bid - stop_level_distance
                if target_close_price > max_sl_price:
                    self._log(
                        f"   ⚠️ SL price too close. Adjusting from {target_close_price:.5f} to {max_sl_price:.5f}")
                    target_close_price = max_sl_price

        # Adjust for SELL positions (TP must be below BID, SL must be above ASK)
        else:  # Grid is net SELL
            # If our target is a Take Profit
            if target_close_price < tick.bid:
                max_tp_price = tick.bid - stop_level_distance
                if target_close_price > max_tp_price:
                    self._log(
                        f"   ⚠️ TP price too close. Adjusting from {target_close_price:.5f} to {max_tp_price:.5f}")
                    target_close_price = max_tp_price
            # If our target is a Stop Loss
            else:
                min_sl_price = tick.ask + stop_level_distance
                if target_close_price < min_sl_price:
                    self._log(
                        f"   ⚠️ SL price too close. Adjusting from {target_close_price:.5f} to {min_sl_price:.5f}")
                    target_close_price = min_sl_price

        # --- 4. Modify Each Position with the Final, Validated Price ---
        all_successful = True
        for p in positions:
            sl_price, tp_price = 0.0, 0.0

            # This logic now correctly uses the market price to determine SL vs TP
            if p.type == mt5.ORDER_TYPE_BUY:
                sl_price, tp_price = (target_close_price, 0.0) if target_close_price < tick.bid else (
                0.0, target_close_price)
            elif p.type == mt5.ORDER_TYPE_SELL:
                sl_price, tp_price = (target_close_price, 0.0) if target_close_price > tick.ask else (
                0.0, target_close_price)

            sl_price = round(sl_price, symbol_info.digits)
            tp_price = round(tp_price, symbol_info.digits)

            if p.sl == sl_price and p.tp == tp_price: continue

            request = {"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "sl": sl_price, "tp": tp_price}
            result = mt5.order_send(request)
            if not (result and result.retcode == mt5.TRADE_RETCODE_DONE):
                self._log(f"   ❌ FAILED to modify position {p.ticket}. Error: {result.comment if result else 'N/A'}")
                all_successful = False

        if all_successful:
            self._log(f"   ✅ Successfully set/updated SL/TP for all positions to unified price.")

        return all_successful

    # def _manage_grid_close_all(self):
    #     symbol, magic = self.settings['symbol'], self.settings['magic_number']
    #
    #     # --- ISOLATION LOGIC ---
    #     hedge_split = 0
    #
    #     hedged_tickets = self._get_hedged_tickets()
    #     all_dir_positions = [p for p in self._get_bot_positions(symbol, magic)]
    #     fight_with_hedge = False
    #     # positions = [p for p in all_dir_positions if p.ticket not in hedged_tickets]
    #     # if len(hedged_tickets) > 0 and len([p for p in all_dir_positions if p.ticket not in hedged_tickets]) >= 4:
    #
    #
    #     # if self.settings.get('hedge_evenly', False):
    #     #     positions = [p for p in all_dir_positions if p.ticket not in hedged_tickets]
    #     # else:
    #     #     positions = all_dir_positions
    #
    #     positions = [p for p in all_dir_positions if p.ticket not in hedged_tickets]
    #
    #     # positions = all_dir_positions
    #
    #     if len(positions) > 4:
    #         fight_with_hedge = True
    #
    #     floating_pnl = sum(p.profit for p in positions)
    #     state = self.trailing_state_all
    #
    #     if floating_pnl > 0:
    #         self.pnl_is_blue = True
    #     else:
    #         self.pnl_is_blue = False
    #
    #     if len(positions) <= 2:
    #         net_profit_target = 10.0
    #     else:
    #         net_profit_target = len(positions) * self.settings.get('net_profit_target_usd', 1.0)
    #
    #     # if self.curfew:
    #     #     net_profit_target = len(positions)
    #     #
    #     # if len(positions) >= 7:
    #     #     net_profit_target = 100
    #
    #     # net_profit_target = self.settings.get('net_profit_target_usd', 1.0)
    #
    #     # if we have observers, work on basic target.
    #     # if len(self._get_rehedged_observer_tickets()):
    #     #     fight_with_hedge = False
    #
    #     # if self.unhedge_loss > 1500:
    #     #     self.unhedge_loss = 0
    #
    #     # --- TRAILING STOP LOGIC ---
    #     if state:
    #         if floating_pnl > self.trailing_state_peak:
    #             self.trailing_state_peak = floating_pnl
    #
    #         activation_target = self.trailing_state_target
    #         drop_amount = self.trailing_state_peak * (self.settings['grid_trailing_drop_percent'] / 100.0)
    #         # current_drop_percent = self._get_dynamic_drop_percent_based_on_target(
    #         #     self.trailing_state_peak,
    #         #     activation_target
    #         # )
    #         # drop_amount = self.trailing_state_peak * (current_drop_percent / 100.0)
    #
    #
    #         # CORRECTED TRAILING LOGIC
    #         # drop_amount = self.trailing_state_peak * (self.settings['grid_trailing_drop_percent'] / 100.0)
    #         # drop_amount = (self.trailing_state_peak - net_profit_target) * (self.settings['grid_trailing_drop_percent'] / 100.0)
    #         closing_threshold = self.trailing_state_peak - drop_amount
    #
    #         if floating_pnl <= closing_threshold:
    #             self._log(
    #                 f"🔻TRAILING STOP HIT at P/L ${floating_pnl:.2f} (Peak was ${self.trailing_state_peak:.2f}). Closing active grid.")
    #             self._close_specific_positions(f"GTS-A", [p.ticket for p in positions])
    #
    #             self._verify_and_clean_hedges()
    #
    #             #self.settings['enable_e2_engine'] = False
    #             # Close all positions associated with that basket
    #             # self._close_all_pending_orders()
    #
    #             # *** THE CRITICAL FIX: RESET THE STATE *AFTER* CLOSING ***
    #             self.trailing_state_all = False
    #             self.trailing_state_peak = 0.0
    #             self.pnl_is_blue = False
    #             self._dynamic_trail_logged = False
    #             self._last_logged_tier = 0
    #             self.l2_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
    #                                    'trade_type': None}
    #             self.l3_trail_state = {'active': False, 'level': 0, 'peak_price': 0, 'lot_size': 0,
    #                                    'trade_type': None}
    #
    #             #return  # End the function here for this cycle.
    #     else:
    #         if floating_pnl >= net_profit_target:
    #             self._log(f"✅ TRAILING ACTIVATED. P/L: ${floating_pnl:.2f}")
    #             self.trailing_state_all = True
    #             self.trailing_state_peak = floating_pnl
    #             self.trailing_state_target = net_profit_target
    #
    #         # activate secondary target
    #         # if floating_pnl > 0 and floating_pnl > 50:
    #         #     self._log(f"✅ Secondary TRAILING ACTIVATED. P/L: ${floating_pnl:.2f}")
    #         #     self.trailing_state_all = True
    #         #     self.trailing_state_peak = floating_pnl
    #         #     self.trailing_state_target = floating_pnl
    #         #     self.trailing_state_source = 'secondary'

    # --- LOW-LEVEL ORDER HELPERS ---
    def _open_market_order(self, l, c, t, return_ticket=False):  # MODIFIED
        s, m = self.settings['symbol'], self.settings['magic_number'];
        if len(c) > 31: c = c[:COMMENT_LENGTH]
        p = mt5.symbol_info_tick(s).ask if t == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(s).bid
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": s, "volume": l, "type": t, "price": p, "magic": m,
               "comment": c, "type_filling": mt5.ORDER_FILLING_IOC}
        res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            self._reset_trailing_grid(soft=True)

            self._log(
                f"   Trade Opened: {c} {'BUY' if t == mt5.ORDER_TYPE_BUY else 'SELL'} {l} lots at ${res.price:,.5f}")
            if return_ticket:
                return res.order  # RETURN THE TICKET NUMBER
        else:
            self._log(f"❌ Market Order Failed: {res.comment if res else 'Unknown Error'}")

        if return_ticket:
            return None  # Return None on failure

    def _place_pending_order(self, l, p, c, t):
        s, m = self.settings['symbol'], self.settings['magic_number'];
        info = mt5.symbol_info(s);
        if not info: return

        # Determine the correct pending order type based on the trade direction
        p_type = mt5.ORDER_TYPE_BUY_LIMIT if t == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_SELL_LIMIT
        if len(c) > 31: c = c[:COMMENT_LENGTH]

        req = {"action": mt5.TRADE_ACTION_PENDING, "symbol": s, "volume": l, "type": p_type,
               "price": round(p, info.digits), "magic": m, "comment": c, "type_filling": mt5.ORDER_FILLING_IOC}

        res = mt5.order_send(req)

        # Essential detailed logging to catch any broker rejection
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            self._log(f"   ✅ Pending Order Placed: {c} at ${p:,.5f}")
        else:
            error_code = res.retcode if res else "N/A"
            error_comment = res.comment if res else "Order Send Failed"
            self._log(f"❌ PENDING ORDER FAILED: {error_comment} (Code: {error_code}) | Target Price: {p:,.5f}")

    def _close_specific_positions(self, c, ticket_list):
        if not ticket_list: return
        self._log(f"   Closing {len(ticket_list)} specific positions. Reason: {c}")
        for ticket in ticket_list: _close_single_position_helper(self, self.settings['symbol'], self.settings['magic_number'],
                                                                 ticket, c)

    def _close_all_pending_orders(self, trade_type=False):
        all_pending = self._get_bot_orders(self.settings['symbol'], self.settings['magic_number'])
        if not all_pending: return; orders_to_delete = []
        if trade_type is not False:
            p_types = {mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP} if trade_type == mt5.ORDER_TYPE_BUY else {
                mt5.ORDER_TYPE_SELL_LIMIT, mt5.ORDER_TYPE_SELL_STOP}
            orders_to_delete = [o for o in all_pending if o.type in p_types]
        else:
            orders_to_delete = all_pending
        if not orders_to_delete: return
        self._log(f"   Deleting {len(orders_to_delete)} pending orders.")
        for o in orders_to_delete: mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})

    def _close_all_positions_and_orders(self, comment):
        self._log(f"   Closing all positions and orders. Reason: {comment}")
        all_pos = self._get_bot_positions(self.settings['symbol'], self.settings['magic_number'])
        self._close_specific_positions(comment, [p.ticket for p in all_pos])
        # self._close_all_pending_orders()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fauji Bot (Exposure Control)")
    parser.add_argument("config_file", type=str, nargs='?', default=None, help="Path to the JSON configuration file.")
    args = parser.parse_args()

    config = default_config
    if args.config_file:
        try:
            with open(args.config_file, 'r') as f:
                config = json.load(f)
        except Exception as e:
            print(f"❌ ERROR loading config file '{args.config_file}': {e}."); sys.exit(1)

    bot = MartingaleBot(config)
    bot.start()
    bot.main_loop()
