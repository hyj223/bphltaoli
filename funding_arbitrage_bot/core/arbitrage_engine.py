#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
套利引擎模块

实现资金费率套利策略的核心逻辑
"""

import asyncio
import logging
import time
import os
import json
import sys  # 添加sys模块导入
from typing import Dict, List, Optional, Any
from datetime import datetime

# 尝试使用包内相对导入（当作为包导入时）
try:
    from funding_arbitrage_bot.exchanges.backpack_api import BackpackAPI
    from funding_arbitrage_bot.exchanges.hyperliquid_api import HyperliquidAPI
    from funding_arbitrage_bot.core.data_manager import DataManager
    from funding_arbitrage_bot.utils.display_manager import DisplayManager
    from funding_arbitrage_bot.utils.webhook_alerter import WebhookAlerter
    from funding_arbitrage_bot.utils.helpers import (
        calculate_funding_diff,
        get_backpack_symbol,
        get_hyperliquid_symbol
    )
# 当直接运行时尝试使用直接导入
except ImportError:
    try:
        from exchanges.backpack_api import BackpackAPI
        from exchanges.hyperliquid_api import HyperliquidAPI
        from core.data_manager import DataManager
        from utils.display_manager import DisplayManager
        from utils.webhook_alerter import WebhookAlerter
        from utils.helpers import (
            calculate_funding_diff,
            get_backpack_symbol,
            get_hyperliquid_symbol
        )
    # 如果以上都失败，尝试相对导入（当在包内运行时）
    except ImportError:
        from ..exchanges.backpack_api import BackpackAPI
        from ..exchanges.hyperliquid_api import HyperliquidAPI
        from ..core.data_manager import DataManager
        from ..utils.display_manager import DisplayManager
        from ..utils.webhook_alerter import WebhookAlerter
        from ..utils.helpers import (
            calculate_funding_diff,
            get_backpack_symbol,
            get_hyperliquid_symbol
        )

class ArbitrageEngine:
    """套利引擎类，负责执行套利策略"""
    
    def __init__(
        self,
        config: Dict[str, Any],
        backpack_api: BackpackAPI,
        hyperliquid_api: HyperliquidAPI,
        logger: Optional[logging.Logger] = None
    ):
        """
        初始化套利引擎
        
        Args:
            config: 配置字典
            backpack_api: Backpack API实例
            hyperliquid_api: Hyperliquid API实例
            logger: 日志记录器
        """
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        
        # 初始化API实例
        self.backpack_api = backpack_api
        self.hyperliquid_api = hyperliquid_api
        
        # 初始化数据管理器
        self.data_manager = DataManager(
            backpack_api=backpack_api,
            hyperliquid_api=hyperliquid_api,
            symbols=config["strategy"]["symbols"],
            funding_update_interval=config["strategy"]["funding_update_interval"],
            logger=self.logger
        )
        
        # 先不初始化显示管理器，等start方法中再初始化
        self.display_manager = None
        
        # 设置策略参数
        strategy_config = config["strategy"]
        open_conditions = strategy_config.get("open_conditions", {})
        close_conditions = strategy_config.get("close_conditions", {})
        
        # 从open_conditions中获取min_funding_diff（新配置结构）
        self.arb_threshold = open_conditions.get("min_funding_diff", 0.00001)
        
        # 如果open_conditions不存在或min_funding_diff不在其中，尝试从旧的配置结构获取
        if self.arb_threshold == 0.00001 and "min_funding_diff" in strategy_config:
            self.arb_threshold = strategy_config["min_funding_diff"]
            self.logger.warning("使用旧配置结构中的min_funding_diff参数")
        
        self.position_sizes = strategy_config.get("position_sizes", {})
        self.max_position_time = close_conditions.get("max_position_time", 28800)  # 默认8小时
        self.trading_pairs = strategy_config.get("trading_pairs", [])
        
        # 价差参数 - 从新配置结构获取
        self.min_price_diff_percent = open_conditions.get("min_price_diff_percent", 0.2)
        self.max_price_diff_percent = open_conditions.get("max_price_diff_percent", 1.0)
        
        # 获取开仓和平仓条件类型
        self.open_condition_type = open_conditions.get("condition_type", "funding_only")
        self.close_condition_type = close_conditions.get("condition_type", "any")
        
        # 平仓条件参数
        self.funding_diff_sign_change = close_conditions.get("funding_diff_sign_change", True)
        self.min_profit_percent = close_conditions.get("min_profit_percent", 0.1)
        self.max_loss_percent = close_conditions.get("max_loss_percent", 0.3)
        self.close_min_funding_diff = close_conditions.get("min_funding_diff", self.arb_threshold / 2)
        
        # 初始化价格和资金费率数据
        self.prices = {}
        self.funding_rates = {}
        self.positions = {}
        self.positions_lock = asyncio.Lock()
        
        # 初始化交易对映射
        self.symbol_mapping = {}
        
        # 资金费率符号记录文件路径
        self.funding_signs_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'data',
            'funding_diff_signs.json'
        )
        
        # 确保data目录存在
        os.makedirs(os.path.dirname(self.funding_signs_file), exist_ok=True)
        
        # 添加开仓时的资金费率符号记录 - 从文件加载
        self.funding_diff_signs = self._load_funding_diff_signs()
        self.logger.info(f"从文件加载资金费率符号记录: {self.funding_diff_signs}")
        
        # 初始化事件循环和任务列表
        self.loop = asyncio.get_event_loop()
        self.tasks = []
        
        # 获取更新间隔
        update_intervals = strategy_config.get("update_intervals", {})
        self.price_update_interval = update_intervals.get("price", 1)
        self.funding_update_interval = update_intervals.get("funding", 60)
        self.position_update_interval = update_intervals.get("position", 10)
        self.check_interval = update_intervals.get("check", 5)
        
        # 初始化统计数据
        self.stats = {
            "total_trades": 0,
            "successful_trades": 0,
            "failed_trades": 0,
            "total_profit": 0,
            "start_time": None,
            "last_trade_time": None
        }

        # 初始化停止事件
        self.stop_event = asyncio.Event()
        
        # 打印配置摘要
        self.logger.info(f"套利引擎初始化完成，套利阈值: {self.arb_threshold}")
        self.logger.info(f"交易对: {self.trading_pairs}")
        self.logger.info(f"价差参数 - 最小: {self.min_price_diff_percent}%, 最大: {self.max_price_diff_percent}%")
        self.logger.info(f"开仓条件类型: {self.open_condition_type}, 平仓条件类型: {self.close_condition_type}")
        
        # 持仓同步参数
        self.position_sync_interval = config.get("strategy", {}).get("position_sync_interval", 300)  # 默认5分钟同步一次
        self.last_sync_time = 0
        
        # 运行标志
        self.is_running = False
        
        # 初始化报警管理器
        order_hook_url = config.get("notification", {}).get("order_webhook_url")
        if order_hook_url:
            self.alerter = WebhookAlerter(order_hook_url)
            self.logger.info(f"已配置订单通知Webhook: {order_hook_url}")
        else:
            self.alerter = None
            self.logger.info("未配置订单通知")

    def _load_funding_diff_signs(self) -> Dict[str, int]:
        """
        从文件加载资金费率符号记录
        
        Returns:
            Dict[str, int]: 资金费率符号记录字典
        """
        try:
            if os.path.exists(self.funding_signs_file):
                with open(self.funding_signs_file, 'r') as f:
                    # 从文件中读取的是字符串形式的字典，需要将键的字符串形式转换为整数
                    signs_data = json.load(f)
                    # 确保符号值是整数类型
                    return {symbol: int(sign) for symbol, sign in signs_data.items()}
            return {}
        except Exception as e:
            self.logger.error(f"加载资金费率符号记录文件失败: {e}")
            return {}
            
    def _save_funding_diff_signs(self) -> None:
        """
        将资金费率符号记录保存到文件
        """
        try:
            with open(self.funding_signs_file, 'w') as f:
                json.dump(self.funding_diff_signs, f)
            self.logger.debug(f"资金费率符号记录已保存到文件: {self.funding_signs_file}")
        except Exception as e:
            self.logger.error(f"保存资金费率符号记录到文件失败: {e}")

    async def start(self):
        """启动套利引擎"""
        try:
            self.logger.info("正在启动套利引擎...")
            
            # 添加调试输出
            print("==== 套利引擎启动 ====", file=sys.__stdout__)
            print(f"策略配置: {self.config.get('strategy', {})}", file=sys.__stdout__)
            print(f"交易对: {self.config.get('strategy', {}).get('symbols', [])}", file=sys.__stdout__)
            
            # 初始化显示管理器
            print("正在初始化显示管理器...", file=sys.__stdout__)
            
            # 确保导入了DisplayManager
            try:
                from funding_arbitrage_bot.utils.display_manager import DisplayManager
            except ImportError:
                try:
                    from utils.display_manager import DisplayManager
                except ImportError:
                    print("无法导入DisplayManager", file=sys.__stdout__)
                    raise
            
            # 创建并启动显示管理器
            self.display_manager = DisplayManager(logger=self.logger)
            print("正在启动显示...", file=sys.__stdout__)
            self.display_manager.start()
            print("显示已启动", file=sys.__stdout__)
            
            # 启动数据流
            await self.data_manager.start_price_feeds()
            
            # 设置运行标志
            self.is_running = True
            
            # 开始主循环
            while self.is_running:
                try:
                    # 更新市场数据显示
                    market_data = self.data_manager.get_all_data()
                    
                    # 获取当前持仓信息
                    bp_positions = await self.backpack_api.get_positions()
                    hl_positions = await self.hyperliquid_api.get_positions()
                    
                    # 添加持仓信息到市场数据中，以便在表格中显示
                    for symbol in market_data:
                        bp_symbol = get_backpack_symbol(symbol)
                        has_position = (bp_symbol in bp_positions) or (symbol in hl_positions)
                        market_data[symbol]["position"] = has_position
                    
                    # 使用直接的系统输出检查数据
                    print(f"更新市场数据: {len(market_data)}项", file=sys.__stdout__)
                    
                    # 更新显示
                    if self.display_manager:
                        self.display_manager.update_market_data(market_data)
                    else:
                        print("显示管理器未初始化", file=sys.__stdout__)
                    
                    # ===== 批量处理模式 =====
                    # 收集需要开仓和平仓的币种
                    open_candidates = []   # 存储满足开仓条件的币种信息
                    close_candidates = []  # 存储满足平仓条件的币种信息
                    
                    # 检查每个交易对的套利机会，但不立即执行开仓/平仓
                    for symbol in self.config["strategy"]["symbols"]:
                        await self._collect_arbitrage_opportunity(
                            symbol, 
                            open_candidates, 
                            close_candidates, 
                            bp_positions, 
                            hl_positions
                        )
                    
                    # 批量执行开仓操作
                    if open_candidates:
                        self.logger.info(f"批量开仓: 共{len(open_candidates)}个币种符合开仓条件")
                        for candidate in open_candidates:
                            try:
                                await self._open_position(
                                    candidate["symbol"],
                                    candidate["funding_diff"],
                                    candidate["bp_funding"],
                                    candidate["hl_funding"],
                                    candidate["available_size"]
                                )
                                # 每次开仓后添加短暂延迟，避免API限制但又不至于等待太久
                                await asyncio.sleep(0.5)
                            except Exception as e:
                                self.logger.error(f"执行{candidate['symbol']}批量开仓时出错: {e}")
                    
                    # 批量执行平仓操作
                    if close_candidates:
                        self.logger.info(f"批量平仓: 共{len(close_candidates)}个币种符合平仓条件")
                        for candidate in close_candidates:
                            try:
                                await self._close_position(
                                    candidate["symbol"],
                                    candidate["position"]
                                )
                                # 每次平仓后添加短暂延迟，避免API限制但又不至于等待太久
                                await asyncio.sleep(0.5)
                            except Exception as e:
                                self.logger.error(f"执行{candidate['symbol']}批量平仓时出错: {e}")
                        
                    # 等待下一次检查
                    await asyncio.sleep(self.config["strategy"]["check_interval"])
                    
                except Exception as e:
                    self.logger.error(f"主循环发生错误: {e}")
                    print(f"主循环错误: {e}", file=sys.__stdout__)
                    await asyncio.sleep(5)  # 发生错误时等待5秒
                    
        except Exception as e:
            self.logger.error(f"启动套利引擎时发生错误: {e}")
            print(f"启动引擎错误: {str(e)}", file=sys.__stdout__)
        finally:
            # 停止显示
            if self.display_manager:
                print("停止显示...", file=sys.__stdout__)
                self.display_manager.stop()
                print("显示已停止", file=sys.__stdout__)
            
    def _analyze_orderbook(self, orderbook, side, amount_usd, price):
        """
        分析订单簿计算滑点
        
        Args:
            orderbook: 订单簿数据
            side: 'bids'表示买单(做多)，'asks'表示卖单(做空)
            amount_usd: 欲交易的美元金额
            price: 当前市场价格
            
        Returns:
            float: 百分比形式的滑点
        """
        try:
            # 添加调试日志
            self.logger.debug(f"分析订单簿: side={side}, amount_usd={amount_usd}, price={price}")
            
            # 检查订单簿数据有效性
            if not orderbook:
                self.logger.warning("订单簿数据为空")
                return 0.1  # 默认较高滑点
                
            if side not in orderbook:
                self.logger.warning(f"订单簿中不存在{side}列表")
                return 0.1  # 默认较高滑点
                
            if not orderbook[side]:
                self.logger.warning(f"订单簿{side}列表为空")
                return 0.1  # 默认较高滑点
            
            # 查看数据结构
            sample_item = orderbook[side][0] if orderbook[side] else None
            self.logger.debug(f"订单簿数据样例: {sample_item}")
            
            # 处理不同交易所可能的数据格式差异
            book_side = []
            for level in orderbook[side]:
                # 对Hyperliquid格式 [{"px": price, "sz": size}, ...] 的处理
                if isinstance(level, dict) and "px" in level and "sz" in level:
                    book_side.append([float(level["px"]), float(level["sz"])])
                # 对Backpack格式 [{"px": price, "sz": size}, ...] 的处理 (已在API中统一)
                elif isinstance(level, dict) and "price" in level and "size" in level:
                    book_side.append([float(level["price"]), float(level["size"])])
                # 对价格和数量已经是列表 [price, size] 的处理
                elif isinstance(level, list) and len(level) >= 2:
                    book_side.append([float(level[0]), float(level[1])])
                else:
                    self.logger.warning(f"无法识别的订单簿数据格式: {level}")
            
            # 如果数据转换后为空，返回默认值
            if not book_side:
                self.logger.warning("数据格式转换后订单簿为空")
                return 0.1  # 默认较高滑点
            
            # 确保订单按价格排序
            if side == 'bids':
                # 买单从高到低
                book_side = sorted(book_side, key=lambda x: float(x[0]), reverse=True)
            else:
                # 卖单从低到高
                book_side = sorted(book_side, key=lambda x: float(x[0]))
            
            # 记录排序后的前几个价格
            self.logger.debug(f"排序后的{side}前5个价格: {[item[0] for item in book_side[:5]]}")
            
            # 计算滑点
            amount_filled = 0.0
            weighted_price = 0.0
            
            levels_checked = 0
            for level in book_side:
                level_price = float(level[0])
                level_size = float(level[1])
                
                # 计算此价格级别的美元价值
                level_value = level_price * level_size
                level_contribution = min(level_value, amount_usd - amount_filled)
                
                self.logger.debug(f"  深度{levels_checked}: 价格={level_price}, 数量={level_size}, 美元价值={level_value}, 贡献={level_contribution}")
                
                if amount_filled + level_value >= amount_usd:
                    # 计算需要从此级别填充的部分
                    remaining = amount_usd - amount_filled
                    size_needed = remaining / level_price
                    
                    # 添加到加权平均价格计算
                    weighted_price += level_price * size_needed
                    amount_filled = amount_usd  # 已完全填充
                    
                    self.logger.debug(f"  已填满订单: 剩余={remaining}, 所需数量={size_needed}, 总填充量={amount_filled}")
                    break
                else:
                    # 添加整个级别到加权平均价格
                    weighted_price += level_price * level_size
                    amount_filled += level_value
                    
                    self.logger.debug(f"  部分填充: 累计填充量={amount_filled}")
                
                levels_checked += 1
                # 只检查前10个深度级别
                if levels_checked >= 10:
                    self.logger.debug("已检查10个深度级别，中断检查")
                    break
            
            # 如果未能完全填充订单，但已填充超过80%，使用已填充部分计算
            if amount_filled < amount_usd:
                fill_percentage = (amount_filled / amount_usd) * 100
                self.logger.warning(f"未能完全填充订单: 已填充{fill_percentage:.2f}% (${amount_filled:.2f}/${amount_usd:.2f})")
                
                if fill_percentage >= 80:
                    self.logger.info(f"已填充{fill_percentage:.2f}%，继续使用已填充部分计算滑点")
                else:
                    # 流动性不足，但不要直接返回固定值，而是基于填充比例计算滑点
                    slippage = (100 - fill_percentage) / 100
                    # 限制最大滑点为0.2%
                    slippage = min(0.2, slippage)
                    self.logger.info(f"流动性不足，基于填充比例计算滑点: {slippage:.4f}")
                    return slippage
            
            # 使用实际填充的金额计算平均价格
            if amount_filled > 0:
                # 原始计算公式有问题，导致加权平均价几乎总是等于市场价
                # average_price = weighted_price / (amount_filled / price)
                
                # 修正后的计算公式：使用最后处理的level_price作为基准
                average_price = weighted_price / (amount_filled / level_price)
            else:
                self.logger.warning("填充金额为0，无法计算滑点")
                return 0.1  # 默认较高滑点
            
            # 计算滑点百分比
            if side == 'bids':
                # 买单，滑点 = (市场价 - 加权平均价) / 市场价
                slippage = (price - average_price) / price * 100
            else:
                # 卖单，滑点 = (加权平均价 - 市场价) / 市场价
                slippage = (average_price - price) / price * 100
                
            # 确保滑点为正值
            slippage = abs(slippage)
            
            self.logger.info(f"计算得到的滑点: {slippage:.4f}%, 市场价: {price}, 加权平均价: {average_price}")
            
            # 限制最小滑点为0.01%，最大滑点为0.5%
            slippage = max(0.01, min(0.5, slippage))
            
            return slippage
            
        except Exception as e:
            import traceback
            self.logger.error(f"分析订单簿计算滑点时出错: {e}")
            self.logger.error(traceback.format_exc())
            return 0.1  # 出错时返回默认滑点

    async def _collect_arbitrage_opportunity(
        self, 
        symbol: str, 
        open_candidates: list, 
        close_candidates: list,
        bp_positions: dict,
        hl_positions: dict
    ):
        """
        收集套利机会，但不立即执行，而是将满足条件的币种添加到候选列表中
        
        Args:
            symbol: 基础币种，如 "BTC"
            open_candidates: 存储满足开仓条件的币种信息的列表
            close_candidates: 存储满足平仓条件的币种信息的列表
            bp_positions: Backpack持仓信息
            hl_positions: Hyperliquid持仓信息
        """
        try:
            # 获取最新数据
            data = await self.data_manager.get_data(symbol)
            
            # 检查数据有效性
            if not self.data_manager.is_data_valid(symbol):
                self.logger.warning(f"{symbol}数据无效，跳过检查")
                return
            
            # 提取价格和资金费率
            bp_data = data["backpack"]
            hl_data = data["hyperliquid"]
            
            bp_price = bp_data["price"]
            bp_funding = bp_data["funding_rate"]
            hl_price = hl_data["price"]
            hl_funding = hl_data["funding_rate"]
            
            # 调整Hyperliquid资金费率以匹配Backpack的8小时周期
            adjusted_hl_funding = hl_funding * 8
            
            # 计算价格差异（百分比）
            price_diff_percent = (bp_price - hl_price) / hl_price * 100
            
            # 计算资金费率差异
            funding_diff, funding_diff_sign = calculate_funding_diff(bp_funding, hl_funding)
            funding_diff_percent = funding_diff * 100  # 转为百分比
            
            bp_symbol = get_backpack_symbol(symbol)
            has_position = (bp_symbol in bp_positions) or (symbol in hl_positions)
            
            # 计算滑点信息
            # 确定做多和做空的交易所
            long_exchange = "hyperliquid" if funding_diff_sign < 0 else "backpack"
            short_exchange = "backpack" if funding_diff_sign < 0 else "hyperliquid"
            
            # 分析订单深度获取滑点信息
            try:
                # 获取交易金额
                trade_size_usd = self.config["strategy"].get("trade_size_usd", {}).get(symbol, 100)
                
                # 获取Hyperliquid订单深度数据
                hl_orderbook = await self.hyperliquid_api.get_orderbook(symbol)
                # 获取Backpack订单深度数据
                bp_orderbook = await self.backpack_api.get_orderbook(symbol)
                
                # 分析订单簿计算精确滑点
                long_slippage = 0.05  # 默认值
                short_slippage = 0.05  # 默认值
                
                # 根据做多/做空交易所计算实际滑点
                if long_exchange == "hyperliquid":
                    long_slippage = self._analyze_orderbook(hl_orderbook, "bids", trade_size_usd, hl_price)
                else:  # long_exchange == "backpack"
                    long_slippage = self._analyze_orderbook(bp_orderbook, "bids", trade_size_usd, bp_price)
                
                if short_exchange == "hyperliquid":
                    short_slippage = self._analyze_orderbook(hl_orderbook, "asks", trade_size_usd, hl_price)
                else:  # short_exchange == "backpack"
                    short_slippage = self._analyze_orderbook(bp_orderbook, "asks", trade_size_usd, bp_price)
                
                # 计算总滑点
                total_slippage = long_slippage + short_slippage
                
                # 将滑点信息添加到市场数据中
                market_data = self.data_manager.get_all_data()
                if symbol in market_data:
                    market_data[symbol]["total_slippage"] = total_slippage
                    market_data[symbol]["long_slippage"] = long_slippage
                    market_data[symbol]["short_slippage"] = short_slippage
                    market_data[symbol]["long_exchange"] = long_exchange
                    market_data[symbol]["short_exchange"] = short_exchange
                    
                    # 调试输出滑点信息
                    self.logger.debug(f"{symbol}滑点分析: 总滑点={total_slippage:.4f}%, 做多({long_exchange})={long_slippage:.4f}%, 做空({short_exchange})={short_slippage:.4f}%")
                    
                    # 更新显示（仅在计算滑点后）
                    if self.display_manager:
                        self.display_manager.update_market_data(market_data)
            except Exception as e:
                self.logger.error(f"计算{symbol}滑点信息时出错: {e}")
            
            # 记录当前状态和调整后的资金费率
            self.logger.info(
                f"{symbol} - 价格差: {price_diff_percent:.4f}%, "
                f"资金费率差: {funding_diff_percent:.6f}%, "
                f"BP: {bp_funding:.6f}(8h), HL原始: {hl_funding:.6f}(1h), HL调整后: {adjusted_hl_funding:.6f}(8h), "
                f"持仓: {'是' if has_position else '否'}"
            )
            
            if not has_position:
                # 没有仓位，检查是否满足开仓条件
                should_open, reason, available_size = self._check_open_conditions_without_execution(
                    symbol, 
                    bp_price, 
                    hl_price, 
                    bp_funding, 
                    adjusted_hl_funding, 
                    price_diff_percent, 
                    funding_diff,
                    bp_positions,
                    hl_positions
                )
                
                if should_open:
                    self.logger.info(f"{symbol} - 决定纳入批量开仓候选，原因: {reason}")
                    # 将满足开仓条件的币种信息添加到候选列表
                    open_candidates.append({
                        "symbol": symbol,
                        "funding_diff": funding_diff,
                        "bp_funding": bp_funding,
                        "hl_funding": adjusted_hl_funding,
                        "available_size": available_size,
                        "reason": reason
                    })
                else:
                    self.logger.debug(f"{symbol} - 不满足开仓条件，跳过")
            else:
                # 有仓位，检查是否满足平仓条件
                bp_position = bp_positions.get(bp_symbol)
                hl_position = hl_positions.get(symbol)
                
                if bp_position and hl_position:
                    should_close, reason, position = self._check_close_conditions_without_execution(
                        symbol, 
                        bp_position, 
                        hl_position, 
                        bp_price, 
                        hl_price, 
                        bp_funding, 
                        adjusted_hl_funding, 
                        price_diff_percent, 
                        funding_diff, 
                        funding_diff_sign
                    )
                    
                    if should_close:
                        self.logger.info(f"{symbol} - 决定纳入批量平仓候选，原因: {reason}")
                        # 将满足平仓条件的币种信息添加到候选列表
                        close_candidates.append({
                            "symbol": symbol,
                            "position": position,
                            "reason": reason
                        })
                    else:
                        self.logger.debug(f"{symbol} - 不满足平仓条件，保持持仓")
        
        except Exception as e:
            self.logger.error(f"收集{symbol}套利机会异常: {e}", exc_info=True)
    
    def _check_open_conditions_without_execution(
        self, 
        symbol: str, 
        bp_price: float, 
        hl_price: float, 
        bp_funding: float, 
        adjusted_hl_funding: float, 
        price_diff_percent: float, 
        funding_diff: float,
        bp_positions: dict,
        hl_positions: dict
    ):
        """
        检查是否满足开仓条件，但不执行开仓
        
        Args:
            symbol: 基础币种，如 "BTC"
            bp_price: Backpack价格
            hl_price: Hyperliquid价格
            bp_funding: Backpack资金费率
            adjusted_hl_funding: 调整后的Hyperliquid资金费率
            price_diff_percent: 价格差异（百分比）
            funding_diff: 资金费率差异
            bp_positions: Backpack持仓信息
            hl_positions: Hyperliquid持仓信息
            
        Returns:
            tuple: (should_open, reason, available_size)
        """
        # 检查是否已有该币种的持仓
        bp_symbol = get_backpack_symbol(symbol)
        has_position = (bp_symbol in bp_positions) or (symbol in hl_positions)
        
        if has_position:
            return False, "已有持仓", 0
            
        # 检查滑点条件
        open_conditions = self.config.get("strategy", {}).get("open_conditions", {})
        max_slippage = open_conditions.get("max_slippage_percent", 0.15)
        ignore_slippage = open_conditions.get("ignore_high_slippage", False)
        
        # 获取市场数据中的滑点信息
        market_data = self.data_manager.get_all_data()
        total_slippage = market_data.get(symbol, {}).get("total_slippage", 0.5)  # 默认为0.5%，较高值
        
        # 检查滑点是否超过限制
        slippage_condition_met = ignore_slippage or total_slippage <= max_slippage
        
        if not slippage_condition_met:
            self.logger.debug(f"{symbol} - 预估滑点({total_slippage:.4f}%)超过最大允许值({max_slippage:.4f}%)，暂不纳入开仓候选")
            return False, f"滑点过高({total_slippage:.4f}%)", 0
            
        # 检查是否达到最大持仓数量限制
        max_positions_count = self.config.get("strategy", {}).get("max_positions_count", 5)
        
        # 统计不同币种的持仓数量
        position_symbols = set()
        
        # 统计Backpack持仓币种
        for pos_symbol in bp_positions:
            base_symbol = pos_symbol.split('_')[0]  # 从"BTC_USDC_PERP"中提取"BTC"
            position_symbols.add(base_symbol)
        
        # 统计Hyperliquid持仓币种
        for pos_symbol in hl_positions:
            position_symbols.add(pos_symbol)
        
        current_positions_count = len(position_symbols)
        
        # 检查是否达到最大持仓数量限制
        if current_positions_count >= max_positions_count:
            self.logger.warning(f"已达到全局最大持仓数量限制({max_positions_count})，当前持仓币种: {position_symbols}，跳过{symbol}开仓")
            if self.display_manager:
                self.display_manager.add_order_message(f"已达全局持仓限制({max_positions_count}币种)，跳过{symbol}开仓")
            return False, f"已达到全局最大持仓数量限制({max_positions_count})", None
        
        # 计算当前持仓的总量，用于检查是否超过最大持仓
        current_size = 0
        
        # 获取交易对配置，检查最大持仓限制
        trading_pair_config = None
        for pair in self.config.get("trading_pairs", []):
            if pair["symbol"] == symbol:
                trading_pair_config = pair
                break
        
        if not trading_pair_config:
            return False, f"未找到{symbol}的交易对配置", None
        
        # 获取最大持仓数量
        max_position_size = trading_pair_config.get("max_position_size", 0)
        
        # 检查是否超过最大持仓限制
        if current_size >= max_position_size:
            self.logger.warning(f"{symbol}当前持仓({current_size})已达到或超过最大持仓限制({max_position_size})，跳过开仓")
            if self.display_manager:
                self.display_manager.add_order_message(f"{symbol}已达最大持仓限制({max_position_size})，跳过开仓")
            return False, f"{symbol}当前持仓({current_size})已达到或超过最大持仓限制({max_position_size})", None
        
        # 计算可用的剩余开仓量
        available_size = max_position_size - current_size
        
        # 获取开仓条件配置
        open_conditions = self.config.get("strategy", {}).get("open_conditions", {})
        condition_type = open_conditions.get("condition_type", "funding_only")
        
        # 检查价格差异条件
        min_price_diff = open_conditions.get("min_price_diff_percent", 0.2)
        max_price_diff = open_conditions.get("max_price_diff_percent", 1.0)
        price_condition_met = min_price_diff <= abs(price_diff_percent) <= max_price_diff
        
        # 检查资金费率差异条件
        min_funding_diff = open_conditions.get("min_funding_diff", 0.00001)
        funding_condition_met = abs(funding_diff) >= min_funding_diff
        
        # 检查滑点条件 
        if not slippage_condition_met:
            self.logger.warning(f"{symbol} - 预估滑点({total_slippage:.4f}%)超过最大允许值({max_slippage:.4f}%)，跳过开仓")
            if self.display_manager:
                self.display_manager.add_order_message(f"{symbol}滑点过高({total_slippage:.4f}%)，跳过开仓")
            return False, f"滑点过高({total_slippage:.4f}%)", 0
            
        # 检查方向一致性
        check_direction_consistency = open_conditions.get("check_direction_consistency", False)
        direction_consistent = True  # 默认方向一致，如果不检查方向一致性，则此条件始终为True
        
        if check_direction_consistency and price_condition_met and funding_condition_met:
            # 获取价格差异和资金费率差异的方向（正负号）
            price_diff_sign = 1 if price_diff_percent > 0 else -1
            funding_diff_sign = 1 if funding_diff > 0 else -1
            
            # 检查方向是否一致（同号）
            direction_consistent = price_diff_sign == funding_diff_sign
            
            if not direction_consistent:
                self.logger.info(
                    f"{symbol} - 方向一致性检查未通过: 价格差异方向({price_diff_percent:.4f}%, 符号={price_diff_sign}) "
                    f"与资金费率差异方向({funding_diff:.6f}, 符号={funding_diff_sign})不一致"
                )
        
        # 根据条件类型决定是否开仓
        should_open = False
        reason = ""
        
        if condition_type == "any":
            # 满足任一条件即可开仓
            should_open = (price_condition_met or funding_condition_met) and direction_consistent
            reason = "满足价格差异或资金费率差异条件"
        elif condition_type == "all":
            # 必须同时满足所有条件才能开仓
            should_open = price_condition_met and funding_condition_met and direction_consistent
            reason = "同时满足价格差异和资金费率差异条件"
        elif condition_type == "funding_only":
            # 仅考虑资金费率条件
            should_open = funding_condition_met and direction_consistent
            reason = "满足资金费率差异条件"
        elif condition_type == "price_only":
            # 仅考虑价格差异条件
            should_open = price_condition_met and direction_consistent
            reason = "满足价格差异条件"
        
        # 记录条件判断结果
        self.logger.info(
            f"{symbol} - 开仓条件检查: 价格条件{'' if price_condition_met else '未'}满足 "
            f"(差异: {abs(price_diff_percent):.4f}%, 阈值: {min_price_diff}%-{max_price_diff}%), "
            f"资金费率条件{'' if funding_condition_met else '未'}满足 "
            f"(差异: {abs(funding_diff):.6f}, 阈值: {min_funding_diff})"
            f"{', 方向一致性检查' + ('' if direction_consistent else '未') + '通过' if check_direction_consistency else ''}"
        )
        
        return should_open, reason, available_size

    def _check_close_conditions_without_execution(
        self,
        symbol: str,
        bp_position: dict,
        hl_position: dict,
        bp_price: float,
        hl_price: float,
        bp_funding: float,
        adjusted_hl_funding: float,
        price_diff_percent: float,
        funding_diff: float,
        funding_diff_sign: int
    ):
        """
        检查是否满足平仓条件，但不执行平仓
        
        Args:
            symbol: 基础币种，如 "BTC"
            bp_position: Backpack持仓信息
            hl_position: Hyperliquid持仓信息
            bp_price: Backpack价格
            hl_price: Hyperliquid价格
            bp_funding: Backpack资金费率
            adjusted_hl_funding: 调整后的Hyperliquid资金费率
            price_diff_percent: 价格差异（百分比）
            funding_diff: 资金费率差异
            funding_diff_sign: 资金费率差异符号
            
        Returns:
            tuple: (should_close, reason, position)
        """
        # 获取平仓条件配置
        close_conditions = self.config.get("strategy", {}).get("close_conditions", {})
        condition_type = close_conditions.get("condition_type", "any")
        
        # 资金费率差异符号变化条件
        funding_sign_change = close_conditions.get("funding_diff_sign_change", True)
        
        # 价格差异符号变化条件
        price_sign_change = close_conditions.get("price_diff_sign_change", False)
        
        # 计算当前价格差异的符号
        price_diff_sign = 1 if price_diff_percent > 0 else -1
        
        # 检查是否存在开仓时记录的资金费率符号
        entry_funding_diff_sign = self.funding_diff_signs.get(symbol)
        
        # 资金费率差异符号变化条件 - 检查符号是否反转
        funding_sign_changed = False
        if entry_funding_diff_sign is not None:
            funding_sign_changed = entry_funding_diff_sign != funding_diff_sign
            self.logger.debug(f"{symbol} - 资金费率符号检查: 开仓时={entry_funding_diff_sign}, 当前={funding_diff_sign}, 变化={funding_sign_changed}")
        
        # 检查是否存在开仓时记录的价格差符号（如果不存在，尝试记录当前符号）
        entry_price_diff_sign = None
        price_sign_changed = False
        
        # 尝试从持仓信息中获取开仓时的价格差符号
        if hasattr(self, 'price_diff_signs') and isinstance(self.price_diff_signs, dict):
            entry_price_diff_sign = self.price_diff_signs.get(symbol)
        else:
            # 如果price_diff_signs不存在，创建它
            self.price_diff_signs = {}
            
        # 如果没有开仓时的价格差符号记录，可能是旧持仓，记录当前符号
        if entry_price_diff_sign is None and symbol in self.funding_diff_signs:
            self.price_diff_signs[symbol] = price_diff_sign
            self.logger.info(f"{symbol} - 未找到开仓时价格差符号记录，记录当前符号: {price_diff_sign}")
            entry_price_diff_sign = price_diff_sign
        
        # 检查价格差符号是否变化
        if entry_price_diff_sign is not None:
            price_sign_changed = entry_price_diff_sign != price_diff_sign
            self.logger.debug(f"{symbol} - 价格差符号检查: 开仓时={entry_price_diff_sign}, 当前={price_diff_sign}, 变化={price_sign_changed}")
        
        # 资金费率差异最小值条件（当差异过小，无套利空间时平仓）
        min_funding_diff = close_conditions.get("min_funding_diff", 0.000005)
        
        # 价格差异条件（获利/止损）
        min_profit_percent = close_conditions.get("min_profit_percent", 0.1)
        max_loss_percent = close_conditions.get("max_loss_percent", 0.3)
        
        # 持仓时间条件
        max_position_time = close_conditions.get("max_position_time", 28800)  # 默认8小时
        
        # 由于我们没有本地持仓记录，无法确切知道开仓时间和开仓时的资金费率
        # 这里主要依赖当前的价格差异和资金费率差异来判断是否平仓
        
        # 检查滑点条件
        max_close_slippage = close_conditions.get("max_close_slippage_percent", 0.25)
        ignore_close_slippage = close_conditions.get("ignore_close_slippage", False)
        
        # 获取市场数据中的滑点信息
        market_data = self.data_manager.get_all_data()
        total_slippage = market_data.get(symbol, {}).get("total_slippage", 0.5)  # 默认为0.5%，较高值
        
        # 检查平仓滑点是否超过限制
        slippage_condition_met = ignore_close_slippage or total_slippage <= max_close_slippage
        
        if not slippage_condition_met:
            self.logger.debug(f"{symbol} - 预估平仓滑点({total_slippage:.4f}%)超过最大允许值({max_close_slippage:.4f}%)，暂不纳入平仓候选")
            return False, f"滑点过高({total_slippage:.4f}%)", None
        
        # 检查各平仓条件
        
        # 1. 资金费率差异符号变化条件
        funding_sign_condition_met = funding_sign_changed and funding_sign_change
        
        # 2. 资金费率差异条件
        funding_value_condition_met = abs(funding_diff) >= min_funding_diff
        
        # 组合资金费率条件
        funding_condition_met = funding_sign_condition_met and funding_value_condition_met
        
        # 3. 价格差异符号变化条件
        if price_sign_change:
            # 如果要求价格差符号反转，则需要差值大于阈值
            price_condition_met = price_sign_changed and abs(price_diff_percent) >= min_profit_percent
        else:
            # 如果不要求价格差符号反转，则只需要差值小于阈值
            price_condition_met = abs(price_diff_percent) < min_profit_percent
        
        # 检查方向一致性
        check_direction_consistency = close_conditions.get("check_direction_consistency", False)
        direction_consistent = True  # 默认方向一致，如果不检查方向一致性，则此条件始终为True
        
        if check_direction_consistency and price_condition_met and funding_condition_met:
            # 获取价格差异和资金费率差异的方向（正负号）
            current_funding_diff_sign = 1 if funding_diff > 0 else -1
            
            # 检查方向是否一致（同号）
            direction_consistent = price_diff_sign == current_funding_diff_sign
            
            if not direction_consistent:
                self.logger.info(
                    f"{symbol} - 平仓方向一致性检查未通过: 价格差异方向({price_diff_percent:.4f}%, 符号={price_diff_sign}) "
                    f"与资金费率差异方向({funding_diff:.6f}, 符号={current_funding_diff_sign})不一致"
                )
        
        # 根据条件类型决定是否平仓
        should_close = False
        reason = ""
        
        if condition_type == "any":
            # 满足任一条件即可平仓
            if funding_condition_met and direction_consistent:
                should_close = True
                reason = f"资金费率符号反转，差异({abs(funding_diff):.6f})超过阈值({min_funding_diff})"
            elif price_condition_met and direction_consistent:
                should_close = True
                if price_sign_change:
                    reason = f"价格差异符号反转，差异({abs(price_diff_percent):.4f}%)超过阈值({min_profit_percent}%)"
                else:
                    reason = f"价格差异({abs(price_diff_percent):.4f}%)小于阈值({min_profit_percent}%)，接近套利完成"
        elif condition_type == "all":
            # 必须同时满足所有条件才能平仓
            should_close = funding_condition_met and price_condition_met and direction_consistent
            reason = "同时满足资金费率差异和价格差异条件"
        elif condition_type == "funding_only":
            # 仅考虑资金费率条件
            should_close = funding_condition_met and direction_consistent
            reason = f"资金费率符号反转，差异({abs(funding_diff):.6f})超过阈值({min_funding_diff})"
        elif condition_type == "price_only":
            # 仅考虑价格差异条件
            should_close = price_condition_met and direction_consistent
            if price_sign_change:
                reason = f"价格差异符号反转，差异({abs(price_diff_percent):.4f}%)超过阈值({min_profit_percent}%)"
            else:
                reason = f"价格差异({abs(price_diff_percent):.4f}%)小于阈值({min_profit_percent}%)，接近套利完成"
        
        # 记录条件判断结果
        self.logger.info(
            f"{symbol} - 平仓条件检查: 资金费率条件{'' if funding_condition_met else '未'}满足 "
            f"(符号反转: {funding_sign_changed}, 差异: {abs(funding_diff):.6f}, 阈值: {min_funding_diff}), "
            f"价格条件{'' if price_condition_met else '未'}满足 "
            f"(差异: {abs(price_diff_percent):.4f}%, "
            f"符号变化: {price_sign_changed if entry_price_diff_sign is not None else '未知'}, "
            f"{'需要符号反转' if price_sign_change else '不需要符号反转'})"
            f"{', 方向一致性检查' + ('' if direction_consistent else '未') + '通过' if check_direction_consistency else ''}"
        )
            
            # 创建持仓对象
        position = {
            "bp_symbol": get_backpack_symbol(symbol),
            "hl_symbol": symbol,
            "bp_side": bp_position["side"],
            "hl_side": hl_position["side"],
            "bp_size": bp_position["size"],
            "hl_size": hl_position["size"]
        }
        
        return should_close, reason, position
    
    async def _open_position(self, symbol: str, funding_diff: float, bp_funding: float, hl_funding: float, available_size: float = None):
        """
        开仓
        
        Args:
            symbol: 基础币种，如 "BTC"
            funding_diff: 资金费率差
            bp_funding: Backpack资金费率
            hl_funding: Hyperliquid资金费率
            available_size: 可用的剩余开仓量，如果为None则使用配置中的开仓数量
        """
        try:
            # 获取最新数据
            data = await self.data_manager.get_data(symbol)
            
            # 获取价格
            bp_price = data["backpack"]["price"]
            hl_price = data["hyperliquid"]["price"]
            
            if bp_price is None or hl_price is None:
                self.logger.error(f"{symbol}价格数据无效，无法开仓")
                return
            
            # 获取交易对配置
            trading_pair_config = None
            for pair in self.config.get("trading_pairs", []):
                if pair["symbol"] == symbol:
                    trading_pair_config = pair
                    break
            
            if not trading_pair_config:
                self.logger.error(f"未找到{symbol}的交易对配置")
                return
            
            # 获取最大持仓数量和最小交易量
            max_position_size = trading_pair_config.get("max_position_size")
            min_volume = trading_pair_config.get("min_volume")
            
            # 计算开仓数量
            bp_size = available_size if available_size is not None else self.position_sizes[symbol]
            hl_size = bp_size  # 两个交易所使用相同的开仓数量
            
            # 检查是否小于最小交易量
            if bp_size < min_volume:
                self.logger.warning(f"{symbol}开仓数量({bp_size})小于最小交易量({min_volume})，已调整为最小交易量")
                bp_size = min_volume
                hl_size = min_volume
            
            # 检查是否超过最大持仓数量
            if max_position_size is not None and (bp_size > max_position_size):
                self.logger.warning(f"{symbol}开仓数量({bp_size})超过最大持仓数量({max_position_size})，已调整为最大持仓数量")
                bp_size = max_position_size
                hl_size = max_position_size
            
            # 计算资金费率差
            funding_diff, funding_diff_sign = calculate_funding_diff(bp_funding, hl_funding)
            
            # 记录当前的资金费率符号用于后续平仓判断
            self.funding_diff_signs[symbol] = funding_diff_sign
            self.logger.debug(f"记录{symbol}开仓时的资金费率符号: {funding_diff_sign}")
            # 保存资金费率符号记录到文件
            self._save_funding_diff_signs()
            
            # 计算价格差异符号并记录
            price_diff_percent = (bp_price - hl_price) / hl_price * 100
            price_diff_sign = 1 if price_diff_percent > 0 else -1
            
            # 确保price_diff_signs字典存在
            if not hasattr(self, 'price_diff_signs'):
                self.price_diff_signs = {}
            
            # 记录当前的价格差符号用于后续平仓判断
            self.price_diff_signs[symbol] = price_diff_sign
            self.logger.debug(f"记录{symbol}开仓时的价格差符号: {price_diff_sign}")
            
            # 准备仓位数据
            bp_symbol = get_backpack_symbol(symbol)  # 使用正确的交易对格式，如 BTC_USDC_PERP
            hl_symbol = get_hyperliquid_symbol(symbol)
            
            # 修正资金费率套利逻辑：资金费率为正时做空，为负时做多
            # 在资金费率为正的交易所做空可以获得资金费率
            # 在资金费率为负的交易所做多可以获得资金费率
            bp_side = "SELL" if bp_funding > 0 else "BUY"
            hl_side = "SELL" if hl_funding > 0 else "BUY"
            
            # 记录资金费率和交易方向
            self.logger.info(f"{symbol} - BP资金费率: {bp_funding:.6f}，方向: {bp_side}；HL资金费率: {hl_funding:.6f}，方向: {hl_side}")
            
            # 获取交易前的持仓状态
            self.logger.info(f"获取{symbol}开仓前的持仓状态")
            pre_bp_positions = await self.backpack_api.get_positions()
            pre_hl_positions = await self.hyperliquid_api.get_positions()
            
            # 记录开仓前的持仓状态
            pre_bp_position = None
            for pos in pre_bp_positions.values():
                if pos.get("symbol") == bp_symbol:
                    pre_bp_position = pos
                    break
                
            pre_hl_position = None
            for pos in pre_hl_positions.values():
                if pos.get("symbol") == hl_symbol:
                    pre_hl_position = pos
                    break
                    
            self.logger.info(f"开仓前持仓: BP {bp_symbol}={pre_bp_position}, HL {hl_symbol}={pre_hl_position}")
            
            # ===== 同时下单 =====
            self.logger.info(f"同时在两个交易所为{symbol}下单: BP {bp_side} {bp_size}, HL {hl_side} {hl_size}")
            
            # 获取价格精度和tick_size
            price_precision = trading_pair_config.get("price_precision", 3)
            tick_size = trading_pair_config.get("tick_size", 0.001)
            
            # 在Hyperliquid下限价单
            hl_price_adjuster = 1.005 if hl_side == "BUY" else 0.995
            hl_limit_price = hl_price * hl_price_adjuster
            
            # 使用正确的tick_size对限价单价格进行调整
            hl_limit_price = round(hl_limit_price / tick_size) * tick_size
            hl_limit_price = round(hl_limit_price, price_precision)
            
            self.logger.info(f"使用限价单开仓Hyperliquid: 价格={hl_limit_price}, 精度={price_precision}, tick_size={tick_size}")
            
            # 同时发送两个交易所的订单
            bp_order_task = asyncio.create_task(
                self.backpack_api.place_order(
                    symbol=bp_symbol,  # 使用正确的交易对格式
                    side=bp_side,
                    size=bp_size,
                    price=None,  # 市价单不需要价格
                    order_type="MARKET"  # Backpack使用市价单
                )
            )
            
            hl_order_task = asyncio.create_task(
                self.hyperliquid_api.place_order(
                    symbol=hl_symbol,
                    side=hl_side,
                    size=hl_size,
                    price=hl_limit_price,
                    order_type="LIMIT"
                )
            )
            
            # 等待订单结果
            bp_result, hl_result = await asyncio.gather(
                bp_order_task,
                hl_order_task,
                return_exceptions=True
            )
            
            # 检查订单结果
            bp_success = not isinstance(bp_result, Exception) and bp_result is not None
            
            # 增强的Hyperliquid订单成功检查逻辑
            hl_success = False
            hl_order_id = None
            if not isinstance(hl_result, Exception):
                if isinstance(hl_result, dict):
                    # 检查直接的success标志
                    if hl_result.get("success", False):
                        hl_success = True
                        hl_order_id = hl_result.get("order_id", "未知")
                        self.logger.info(f"Hyperliquid订单成功，订单ID: {hl_order_id}")
                        
                        # 检查订单是否已立即成交
                        if hl_result.get("status") == "filled":
                            self.logger.info(f"Hyperliquid订单已立即成交，均价: {hl_result.get('price', '未知')}")
                    
                    # 检查是否包含filled状态
                    elif "raw_response" in hl_result:
                        raw_response = hl_result["raw_response"]
                        raw_str = json.dumps(raw_response)
                        
                        if "filled" in raw_str:
                            self.logger.info("检测到订单可能已成交，尝试提取订单信息")
                            hl_success = True
                            
                            # 尝试提取订单ID
                            try:
                                if isinstance(raw_response, dict) and "response" in raw_response:
                                    response_data = raw_response["response"]
                                    if "data" in response_data and "statuses" in response_data["data"]:
                                        statuses = response_data["data"]["statuses"]
                                        if statuses and "filled" in statuses[0]:
                                            hl_order_id = statuses[0]["filled"].get("oid", "未知")
                                            self.logger.info(f"成功提取订单ID: {hl_order_id}")
                            except Exception as extract_error:
                                self.logger.error(f"提取订单ID时出错: {extract_error}")
                                hl_order_id = "未能提取"
            
            # 日志记录订单结果
            if bp_success:
                self.logger.info(f"Backpack下单成功: {bp_result}")
            else:
                self.logger.error(f"Backpack下单失败: {bp_result}")
                
            if hl_success:
                self.logger.info(f"Hyperliquid下单成功: {hl_order_id}")
            else:
                self.logger.error(f"Hyperliquid下单失败: {hl_result}")
            
            # ===== 验证持仓变化 =====
            # 等待3秒让交易所处理订单
            self.logger.info("等待3秒让交易所处理订单...")
            await asyncio.sleep(3)
            
            # 获取交易后的持仓状态
            self.logger.info(f"获取{symbol}开仓后的持仓状态")
            post_bp_positions = await self.backpack_api.get_positions()
            post_hl_positions = await self.hyperliquid_api.get_positions()
            
            # 记录开仓后的持仓状态
            post_bp_position = None
            for pos in post_bp_positions.values():
                if pos.get("symbol") == bp_symbol:
                    post_bp_position = pos
                    break
                
            post_hl_position = None
            for pos in post_hl_positions.values():
                if pos.get("symbol") == hl_symbol:
                    post_hl_position = pos
                    break
                
            self.logger.info(f"开仓后持仓: BP {bp_symbol}={post_bp_position}, HL {hl_symbol}={post_hl_position}")
            
            # 验证持仓变化
            bp_position_changed = False
            hl_position_changed = False
            
            # 检查Backpack持仓变化
            if pre_bp_position is None and post_bp_position is not None:
                # 新建立了持仓
                bp_position_changed = True
                self.logger.info(f"Backpack成功建立{bp_symbol}新持仓: {post_bp_position}")
            elif pre_bp_position is not None and post_bp_position is not None:
                # 检查持仓大小是否变化
                pre_size = float(pre_bp_position.get("quantity", 0))
                post_size = float(post_bp_position.get("quantity", 0))
                if abs(post_size - pre_size) >= 0.8 * bp_size:  # 允许80%的差异容忍度
                    bp_position_changed = True
                    self.logger.info(f"Backpack {bp_symbol}持仓量变化: {pre_size} -> {post_size}")
            
            # 检查Hyperliquid持仓变化
            if pre_hl_position is None and post_hl_position is not None:
                # 新建立了持仓
                hl_position_changed = True
                self.logger.info(f"Hyperliquid成功建立{hl_symbol}新持仓: {post_hl_position}")
            elif pre_hl_position is not None and post_hl_position is not None:
                # 检查持仓大小是否变化
                pre_size = float(pre_hl_position.get("size", 0))
                post_size = float(post_hl_position.get("size", 0))
                if abs(post_size - pre_size) >= 0.8 * hl_size:  # 允许80%的差异容忍度
                    hl_position_changed = True
                    self.logger.info(f"Hyperliquid {hl_symbol}持仓量变化: {pre_size} -> {post_size}")
            
            # 根据持仓变化情况判断开仓成功与否
            if bp_position_changed and hl_position_changed:
                # 两个交易所都成功开仓
                message = (
                    f"开仓成功: \n"
                    f"Backpack: {bp_side} {bp_size} {bp_symbol}\n"
                    f"Hyperliquid: {hl_side} {hl_size} {hl_symbol}"
                )
                self.logger.info(message)
                self.display_manager.add_order_message(message)
                # 更新订单统计
                self.display_manager.update_order_stats("open", True)
                
                # 发送通知
                if self.alerter:
                    self.alerter.send_order_notification(
                        symbol=symbol,
                        action="开仓",
                        quantity=bp_size,
                        price=bp_price,
                        side="多" if bp_side == "BUY" else "空",
                        exchange="Backpack"
                    )
                return True
            elif bp_position_changed and not hl_position_changed:
                # 只有Backpack成功开仓，尝试平掉单边持仓
                self.logger.warning(f"{symbol}只在Backpack开仓成功，尝试关闭单边持仓")
                try:
                    await self.backpack_api.close_position(bp_symbol)
                    self.logger.info(f"已关闭Backpack上的{bp_symbol}单边持仓")
                except Exception as e:
                    self.logger.error(f"关闭Backpack单边持仓失败: {e}")
                # 更新订单统计
                self.display_manager.update_order_stats("open", False)
                return False
            
            elif not bp_position_changed and hl_position_changed:
                # 只有Hyperliquid成功开仓，尝试平掉单边持仓
                self.logger.warning(f"{symbol}只在Hyperliquid开仓成功，尝试关闭单边持仓")
                try:
                    close_side = "BUY" if hl_side == "SELL" else "SELL"
                    await self.hyperliquid_api.place_order(
                        symbol=hl_symbol,
                        side=close_side,
                        size=hl_size,
                        price=None,
                        order_type="MARKET"
                    )
                    self.logger.info(f"已关闭Hyperliquid上的{hl_symbol}单边持仓")
                except Exception as e:
                    self.logger.error(f"关闭Hyperliquid单边持仓失败: {e}")
                # 更新订单统计
                self.display_manager.update_order_stats("open", False)
                return False
            else:
                # 两个交易所都未成功开仓
                self.logger.error(f"{symbol}在两个交易所均未成功开仓")
                # 更新订单统计
                self.display_manager.update_order_stats("open", False)
                return False
                
        except Exception as e:
            self.logger.error(f"{symbol}开仓过程发生异常: {e}")
            self.display_manager.add_order_message(f"{symbol}开仓过程发生异常: {e}")
            return False
    
    async def _close_position(self, symbol: str, position: Dict[str, Any]):
        """
        平仓
        
        Args:
            symbol: 基础币种，如 "BTC"
            position: 仓位数据字典
        """
        try:
            message = f"尝试为{symbol}平仓"
            self.logger.info(message)
            self.display_manager.add_order_message(message)
            
            # 获取仓位信息
            bp_symbol = position["bp_symbol"]
            hl_symbol = position["hl_symbol"]
            bp_side = position["bp_side"]
            hl_side = position["hl_side"]
            bp_size = float(position["bp_size"])  # 确保是浮点数
            hl_size = float(position["hl_size"])  # 确保是浮点数
            
            # 平仓方向与开仓方向相反
            bp_close_side = "SELL" if bp_side == "BUY" else "BUY"
            hl_close_side = "SELL" if hl_side == "BUY" else "BUY"
            
            message = (
                f"平仓方向: BP {bp_close_side} {bp_size} {bp_symbol}, "
                f"HL {hl_close_side} {hl_size} {hl_symbol}"
            )
            self.logger.info(message)
            self.display_manager.add_order_message(message)
            
            # 获取当前价格
            bp_price = await self.backpack_api.get_price(bp_symbol)
            if not bp_price:
                message = f"无法获取{bp_symbol}的当前价格，平仓失败"
                self.logger.error(message)
                self.display_manager.add_order_message(message)
                return False
            
            # 获取交易前的持仓状态
            self.logger.info(f"获取{symbol}平仓前的持仓状态")
            pre_bp_positions = await self.backpack_api.get_positions()
            pre_hl_positions = await self.hyperliquid_api.get_positions()
            
            # 记录平仓前的持仓状态
            pre_bp_position = None
            for pos in pre_bp_positions.values():
                if pos.get("symbol") == bp_symbol:
                    pre_bp_position = pos
                    break
                
            pre_hl_position = None
            for pos in pre_hl_positions.values():
                if pos.get("symbol") == hl_symbol:
                    pre_hl_position = pos
                    break
                
            self.logger.info(f"平仓前持仓: BP {bp_symbol}={pre_bp_position}, HL {hl_symbol}={pre_hl_position}")
            
            # 如果任一交易所没有持仓，则无需平仓
            if pre_bp_position is None:
                self.logger.warning(f"Backpack没有{bp_symbol}的持仓，无需平仓")
                return False
            
            if pre_hl_position is None:
                self.logger.warning(f"Hyperliquid没有{hl_symbol}的持仓，无需平仓")
                return False
                
            # 根据买卖方向调整价格确保快速成交
            bp_price_adjuster = 1.005 if bp_close_side == "BUY" else 0.995
            bp_limit_price = bp_price * bp_price_adjuster
            
            # 根据tick_size调整价格
            bp_limit_price = round(bp_limit_price / tick_size) * tick_size
            
            # 控制小数位数，确保不超过配置的精度
            bp_limit_price = round(bp_limit_price, price_precision)
            
            self.logger.info(f"平仓价格计算: 原始价格={bp_price}, 调整系数={bp_price_adjuster}, "
                            f"调整后价格={bp_limit_price}, 精度={price_precision}, tick_size={tick_size}")
            
            # 同时平仓
            bp_order_task = asyncio.create_task(
                self.backpack_api.place_order(
                    symbol=bp_symbol,
                    side=bp_close_side,
                    size=float(bp_size),  # 确保size是浮点数
                    price=None,  # 使用市价单简化操作
                    order_type="MARKET"
                )
            )
            
            # 在Hyperliquid也使用市价单平仓
            hl_order_task = asyncio.create_task(
                self.hyperliquid_api.place_order(
                    symbol=hl_symbol,
                    side=hl_close_side,
                    size=float(hl_size),  # 确保size是浮点数
                    price=None,  # 价格会在API内部计算
                    order_type="MARKET"  # 使用市价单简化操作
                )
            )
            
            # 等待平仓结果
            bp_result, hl_result = await asyncio.gather(
                bp_order_task,
                hl_order_task,
                return_exceptions=True
            )
            
            # 检查平仓结果
            bp_success = not isinstance(bp_result, Exception) and not (isinstance(bp_result, dict) and bp_result.get("error"))
            
            # 增强的Hyperliquid订单成功检查逻辑
            hl_success = False
            if not isinstance(hl_result, Exception):
                if isinstance(hl_result, dict):
                    # 检查直接的success标志
                    if hl_result.get("success", False):
                        hl_success = True
                        hl_order_id = hl_result.get("order_id", "未知")
                        self.logger.info(f"Hyperliquid平仓订单成功，订单ID: {hl_order_id}")
                        
                        # 检查订单是否已立即成交
                        if hl_result.get("status") == "filled":
                            self.logger.info(f"Hyperliquid平仓订单已立即成交，均价: {hl_result.get('price', '未知')}")
                    
                    # 检查是否包含filled状态
                    elif "raw_response" in hl_result:
                        raw_response = hl_result["raw_response"]
                        raw_str = json.dumps(raw_response)
                        
                        if "filled" in raw_str:
                            self.logger.info("检测到平仓订单可能已成交")
                            hl_success = True
            
            # 日志记录订单结果
            if bp_success:
                self.logger.info(f"Backpack平仓订单成功: {bp_result}")
            else:
                self.logger.error(f"Backpack平仓订单失败: {bp_result}")
            
            if hl_success:
                self.logger.info(f"Hyperliquid平仓订单成功")
            else:
                self.logger.error(f"Hyperliquid平仓订单失败: {hl_result}")
            
            # ===== 验证持仓变化 =====
            # 等待3秒让交易所处理订单
            self.logger.info("等待3秒让交易所处理订单...")
            await asyncio.sleep(3)
            
            # 获取交易后的持仓状态
            self.logger.info(f"获取{symbol}平仓后的持仓状态")
            post_bp_positions = await self.backpack_api.get_positions()
            post_hl_positions = await self.hyperliquid_api.get_positions()
            
            # 记录平仓后的持仓状态
            post_bp_position = None
            for pos in post_bp_positions.values():
                if pos.get("symbol") == bp_symbol:
                    post_bp_position = pos
                    break
                
            post_hl_position = None
            for pos in post_hl_positions.values():
                if pos.get("symbol") == hl_symbol:
                    post_hl_position = pos
                    break
                
            self.logger.info(f"平仓后持仓: BP {bp_symbol}={post_bp_position}, HL {hl_symbol}={post_hl_position}")
            
            # 验证持仓变化
            bp_position_closed = False
            hl_position_closed = False
            
            # 检查Backpack持仓变化
            if pre_bp_position is not None and post_bp_position is None:
                # 持仓已完全平掉
                bp_position_closed = True
                self.logger.info(f"Backpack成功平掉{bp_symbol}全部持仓")
            elif pre_bp_position is not None and post_bp_position is not None:
                # 检查持仓大小是否变化
                pre_size = float(pre_bp_position.get("quantity", 0))
                post_size = float(post_bp_position.get("quantity", 0))
                if pre_size > 0 and (pre_size - post_size) / pre_size >= 0.9:  # 平掉了90%以上的持仓
                    bp_position_closed = True
                    self.logger.info(f"Backpack {bp_symbol}持仓量显著减少: {pre_size} -> {post_size}")
            
            # 检查Hyperliquid持仓变化
            if pre_hl_position is not None and post_hl_position is None:
                # 持仓已完全平掉
                hl_position_closed = True
                self.logger.info(f"Hyperliquid成功平掉{hl_symbol}全部持仓")
            elif pre_hl_position is not None and post_hl_position is not None:
                # 检查持仓大小是否变化
                pre_size = float(pre_hl_position.get("size", 0))
                post_size = float(post_hl_position.get("size", 0))
                if pre_size > 0 and (pre_size - post_size) / pre_size >= 0.9:  # 平掉了90%以上的持仓
                    hl_position_closed = True
                    self.logger.info(f"Hyperliquid {hl_symbol}持仓量显著减少: {pre_size} -> {post_size}")
            
            # 根据持仓变化情况判断平仓成功与否
            if bp_position_closed and hl_position_closed:
                # 两个交易所都成功平仓
                message = f"{symbol}平仓成功"
                self.logger.info(message)
                self.display_manager.add_order_message(message)
                # 更新订单统计
                self.display_manager.update_order_stats("close", True)
                
                # 清除资金费率符号记录
                if symbol in self.funding_diff_signs:
                    del self.funding_diff_signs[symbol]
                    self.logger.debug(f"已清除{symbol}的资金费率符号记录")
                    # 保存更新后的资金费率符号记录到文件
                    self._save_funding_diff_signs()
                
                # 发送通知
                if self.alerter:
                    self.alerter.send_order_notification(
                        symbol=symbol,
                        action="平仓",
                        quantity=bp_size,
                        price=bp_price,
                        side="多" if bp_close_side == "BUY" else "空",
                        exchange="Backpack"
                    )
                return True
            elif (not bp_position_closed and hl_position_closed) or (bp_position_closed and not hl_position_closed):
                # 单边平仓成功，可能需要尝试再次平掉另一边
                self.logger.warning(f"{symbol}单边平仓成功，另一边可能需要手动处理")
                # 更新订单统计
                self.display_manager.update_order_stats("close", False)
                # 这里可以添加重试逻辑
                return False
            else:
                # 两个交易所都未成功平仓
                self.logger.error(f"{symbol}在两个交易所均未成功平仓")
                # 更新订单统计
                self.display_manager.update_order_stats("close", False)
                return False
                    
        except Exception as e:
            message = f"{symbol}平仓异常: {e}"
            self.logger.error(message)
            self.display_manager.add_order_message(message) 
            return False 