# API 参考文档

## 概述

A 股 AI 投研系统提供完整的 RESTful API，用于股票研究、决策预测、回测和资产管理。

**Base URL**: `http://localhost:8000`  
**API Version**: v1  
**更新日期**: 2026-09-20

---

## 认证

所有 API 请求需要在 Cookie 中携带有效的 session token。

### 登录

```http
POST /api/v1/auth/login
Content-Type: application/json

{
  "username": "admin",
  "password": "your_password"
}
```

**响应**:
```json
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "token_type": "bearer",
  "user": {
    "user_id": 1,
    "username": "admin",
    "is_admin": true
  }
}
```

### 登出

```http
POST /api/v1/auth/logout
```

---

## 决策预测 API

### 1. 单个股票决策预测

**新增**: Jev 模型支持

```http
POST /api/v1/decision/predict
Content-Type: application/json

{
  "symbol": "000001.SZ",
  "trading_date": "2026-09-20",
  "mode": "jev"
}
```

**参数**:
- `symbol` (required): 股票代码，格式 `XXXXXX.(SH|SZ|BJ)`
- `trading_date` (optional): 交易日期，默认今天
- `mode` (optional): 决策模式 `legacy` | `jev`，默认使用系统配置

**响应**:
```json
{
  "decision": {
    "symbol": "000001.SZ",
    "trading_date": "2026-09-20",
    "direction_1d": "UP",
    "direction_5d": "UP",
    "up_over_3pct_5d": true,
    "action": "BUY",
    "risk": "MEDIUM",
    "position": 50,
    "confidence": {
      "direction_1d": 0.72,
      "direction_5d": 0.68,
      "up_over_3pct_5d": 0.85,
      "action": 0.75,
      "risk": 0.63,
      "position": 0.58
    },
    "model_version": "jev-auto_2026-09-20"
  },
  "status": "success"
}
```

### 2. 批量决策预测

**新增**: 批量预测接口

```http
POST /api/v1/decision/batch
Content-Type: application/json

{
  "symbols": ["000001.SZ", "600000.SH", "000858.SZ"],
  "trading_date": "2026-09-20",
  "mode": "jev"
}
```

**限制**: 单次最多 100 个股票

**响应**:
```json
{
  "decisions": {
    "000001.SZ": {
      "direction_1d": "UP",
      "action": "BUY",
      "position": 50
    },
    "600000.SH": {
      "direction_1d": "FLAT",
      "action": "HOLD",
      "position": 30
    },
    "000858.SZ": {
      "direction_1d": "DOWN",
      "action": "SELL",
      "position": 0
    }
  },
  "status": "success",
  "failed_symbols": []
}
```

### 3. 获取决策模式

**新增**: 查询当前决策配置

```http
GET /api/v1/decision/mode
```

**响应**:
```json
{
  "decision_mode": "jev",
  "fallback_enabled": true,
  "jev_model_version": "auto_2026-09-20",
  "jev_device": "cpu"
}
```

### 4. 切换决策模式

**新增**: 运行时切换模式

```http
POST /api/v1/decision/mode
Content-Type: application/json

{
  "mode": "jev",
  "fallback_enabled": true
}
```

**响应**: 同 GET `/api/v1/decision/mode`

### 5. 列出可用模型

**新增**: 查询所有已训练模型

```http
GET /api/v1/decision/models
```

**响应**:
```json
{
  "models": [
    {
      "version": "legacy-v1.0.0",
      "path": "builtin",
      "mode": "legacy"
    },
    {
      "version": "auto_2026-09-20",
      "path": "/data/models/jev/auto_2026-09-20",
      "mode": "jev"
    },
    {
      "version": "manual_v2.2.0",
      "path": "/data/models/jev/manual_v2.2.0",
      "mode": "jev"
    }
  ],
  "current_version": "auto_2026-09-20",
  "status": "success"
}
```

---

## 训练管理 API

### 1. 触发自动训练

**新增**: 手动触发 Jev 模型训练

```http
POST /api/v1/training/jev/trigger
Content-Type: application/json

{
  "force": false,
  "dataset_config": {
    "train_days": 730,
    "val_days": 60,
    "test_days": 30
  }
}
```

**响应**:
```json
{
  "status": "success",
  "training_id": "train_20260920_153045",
  "estimated_duration_minutes": 120,
  "message": "Training job queued"
}
```

### 2. 查询训练状态

**新增**: 查询训练任务进度

```http
GET /api/v1/training/jev/status/{training_id}
```

**响应**:
```json
{
  "training_id": "train_20260920_153045",
  "status": "running",
  "progress": 0.35,
  "current_epoch": 35,
  "total_epochs": 100,
  "metrics": {
    "train_loss": 1.234,
    "val_loss": 1.456,
    "val_accuracy": {
      "direction_1d": 0.55,
      "action": 0.60
    }
  },
  "started_at": "2026-09-20T15:30:45Z",
  "estimated_completion": "2026-09-20T17:30:45Z"
}
```

### 3. 取消训练

**新增**: 取消正在进行的训练

```http
POST /api/v1/training/jev/cancel/{training_id}
```

### 4. 训练历史

**新增**: 查询历史训练记录

```http
GET /api/v1/training/jev/history?limit=10&offset=0
```

**响应**:
```json
{
  "trainings": [
    {
      "training_id": "train_20260920_153045",
      "status": "completed",
      "model_version": "auto_2026-09-20",
      "started_at": "2026-09-20T15:30:45Z",
      "completed_at": "2026-09-20T17:25:12Z",
      "duration_minutes": 115,
      "final_metrics": {
        "val_loss": 1.123,
        "val_accuracy": {
          "direction_1d": 0.58,
          "action": 0.62
        }
      }
    }
  ],
  "total": 15
}
```

---

## 市场数据 API

### 实时行情

```http
GET /api/v1/market/quotes?symbols=000001.SZ,600000.SH
```

**响应**:
```json
{
  "quotes": {
    "000001.SZ": {
      "symbol": "000001.SZ",
      "name": "平安银行",
      "price": 12.34,
      "change": 0.12,
      "change_pct": 0.98,
      "volume": 123456789,
      "amount": 1523456789.0,
      "timestamp": "2026-09-20T15:00:00Z"
    }
  }
}
```

### K线数据

**优化**: 支持更多周期

```http
GET /api/v1/market/kline/{symbol}?period=day&limit=160
```

**参数**:
- `period`: `1min` | `5min` | `15min` | `30min` | `60min` | `day` | `week` | `month`
- `limit`: 返回条数，默认 160

### 大盘指数

```http
GET /api/v1/market/indices
```

**响应**:
```json
{
  "quotes": [
    {
      "symbol": "000300.SH",
      "name": "沪深300",
      "price": 3456.78,
      "change_pct": 0.45
    },
    {
      "symbol": "000905.SH",
      "name": "中证500",
      "price": 5678.90,
      "change_pct": -0.23
    }
  ],
  "labels": {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000"
  }
}
```

---

## 研究与回测 API

### 发起研究

```http
POST /api/v1/research/runs
Content-Type: application/json

{
  "symbols": ["000001.SZ", "600000.SH"],
  "trading_date": "2026-09-20",
  "decision_mode": "jev"
}
```

### 查询研究结果

```http
GET /api/v1/research/runs/{run_id}
```

### 回测

```http
POST /api/v1/backtest
Content-Type: application/json

{
  "start_date": "2026-01-01",
  "end_date": "2026-09-20",
  "symbols": ["000001.SZ", "600000.SH"],
  "initial_capital": 100000,
  "strategy": "jev_multi_task"
}
```

---

## 资产管理 API

### 获取持仓

```http
GET /api/v1/assets/positions
```

### 添加持仓

```http
POST /api/v1/assets/positions
Content-Type: application/json

{
  "symbol": "000001.SZ",
  "quantity": 1000,
  "cost": 12.50
}
```

### 自选股管理

```http
GET /api/v1/assets/watchlist
POST /api/v1/assets/watchlist
DELETE /api/v1/assets/watchlist/{symbol}
```

---

## 错误码

| 状态码 | 说明 |
|-------|------|
| 200 | 成功 |
| 400 | 请求参数错误 |
| 401 | 未认证 |
| 403 | 无权限 |
| 404 | 资源不存在 |
| 429 | 请求过于频繁 |
| 500 | 服务器内部错误 |
| 501 | 功能未实现 |

**错误响应格式**:
```json
{
  "detail": "Invalid symbol format",
  "error_code": "INVALID_SYMBOL",
  "status_code": 400
}
```

---

## 速率限制

| 端点 | 限制 |
|------|------|
| `/api/v1/auth/login` | 10 次/分钟 |
| `/api/v1/decision/predict` | 30 次/分钟 |
| `/api/v1/decision/batch` | 10 次/分钟 |
| `/api/v1/market/quotes` | 60 次/分钟 |
| `/api/v1/training/*` | 5 次/小时 |
| 其他端点 | 120 次/分钟 |

---

## WebSocket API

### 实时行情订阅

**新增**: WebSocket 实时推送

```javascript
const ws = new WebSocket('ws://localhost:8000/api/v1/market/ws')

// 订阅行情
ws.send(JSON.stringify({
  action: 'subscribe',
  symbols: ['000001.SZ', '600000.SH']
}))

// 接收实时数据
ws.onmessage = (event) => {
  const data = JSON.parse(event.data)
  console.log(data.quotes)
}
```

---

## SDK 示例

### Python

```python
import requests

class AShareAPI:
    def __init__(self, base_url='http://localhost:8000'):
        self.base_url = base_url
        self.session = requests.Session()
    
    def login(self, username, password):
        resp = self.session.post(
            f'{self.base_url}/api/v1/auth/login',
            json={'username': username, 'password': password}
        )
        return resp.json()
    
    def predict(self, symbol, mode='jev'):
        resp = self.session.post(
            f'{self.base_url}/api/v1/decision/predict',
            json={'symbol': symbol, 'mode': mode}
        )
        return resp.json()

# 使用示例
api = AShareAPI()
api.login('admin', 'password')
decision = api.predict('000001.SZ', mode='jev')
print(decision['decision']['action'])  # BUY
```

### JavaScript

```javascript
class AShareAPI {
  constructor(baseURL = 'http://localhost:8000') {
    this.baseURL = baseURL
  }
  
  async login(username, password) {
    const resp = await fetch(`${this.baseURL}/api/v1/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
      credentials: 'include'
    })
    return resp.json()
  }
  
  async predict(symbol, mode = 'jev') {
    const resp = await fetch(`${this.baseURL}/api/v1/decision/predict`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, mode }),
      credentials: 'include'
    })
    return resp.json()
  }
}

// 使用示例
const api = new AShareAPI()
await api.login('admin', 'password')
const decision = await api.predict('000001.SZ', 'jev')
console.log(decision.decision.action)  // 'BUY'
```

---

**最后更新**: 2026-09-20  
**维护者**: A 股 AI 投研团队
