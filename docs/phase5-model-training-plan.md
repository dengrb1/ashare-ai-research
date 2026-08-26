# Phase 5: 模型训练集成

## 目标

评估并决定是否需要集成模型训练能力，如果需要，将训练流程与研究系统打通。

## 前置分析

### 当前系统能力 (Phase 1-4)

- ✅ 评分模型 (`src/ashare_ai/scoring/`)
- ✅ 回测引擎 (`src/ashare_ai/backtest/`)
- ✅ 特征工程 (`src/ashare_ai/features/`)
- ✅ 数据管道 (Phase 4)
- ✅ 任务编排系统

### 需要回答的问题

1. **现有评分模型如何工作？**
   - 是基于规则的？
   - 是预训练的机器学习模型？
   - 是否支持在线更新？

2. **是否需要训练新模型？**
   - 研究报告生成是否需要 ML 模型？
   - 评分系统是否需要持续学习？
   - 回测结果是否用于模型优化？

3. **训练基础设施需求**
   - 是否需要 GPU？
   - 是否需要分布式训练？
   - 训练频率如何（定时/触发）？

## Phase 5A: 现状审查

### 实施步骤

1. **审查评分模块**
   ```bash
   # 检查评分模型实现
   grep -r "train\|fit\|model" src/ashare_ai/scoring/
   grep -r "sklearn\|torch\|tensorflow\|xgboost" src/ashare_ai/scoring/
   ```

2. **检查是否存在训练代码**
   ```bash
   # 寻找训练相关文件
   find src/ashare_ai -name "*train*" -o -name "*fit*" -o -name "*model*"
   ```

3. **检查依赖**
   ```bash
   # 查看是否有 ML 库
   grep -E "scikit-learn|torch|tensorflow|xgboost|lightgbm|catboost" pyproject.toml
   ```

4. **确认数据存储**
   - 是否有模型存储路径？
   - 是否有训练数据存储？
   - 是否有模型版本管理？

### 决策点

根据审查结果，决定以下方向之一：

**方向 A**: 系统使用基于规则的评分，无需训练
- → **跳过 Phase 5**，标记为 N/A
- → 直接进入 Phase 6

**方向 B**: 系统使用预训练模型，但不需要重新训练
- → **简化 Phase 5**：仅添加模型加载和推理集成
- → 将模型文件存储到 `models/` 目录
- → 在 compose.yaml 添加模型存储卷

**方向 C**: 系统需要定期训练/微调模型
- → **完整 Phase 5**：实现训练流程、模型管理、版本控制

## Phase 5B: 模型训练集成（如果需要）

### 前提条件
- 已完成 Phase 5A 审查
- 确认需要训练能力（方向 C）

### 5B.1 训练任务定义

**设计原则**:
- 训练作为长时间后台任务
- 使用 Redis 队列编排
- 子进程隔离执行
- 支持 checkpoint 恢复

**实施步骤**:

1. **添加训练任务类型**
   - 在 `orchestration/redis_queue.py` 添加 `model-training` 队列
   - 创建 `orchestration/training_jobs.py`
   - 实现训练任务 handler：
     ```python
     def execute_training_job(job_id: str) -> None:
         """Execute model training job."""
         # 1. 加载训练配置
         # 2. 准备训练数据
         # 3. 初始化模型
         # 4. 训练循环（支持 checkpoint）
         # 5. 验证模型
         # 6. 保存模型
         # 7. 更新数据库
     ```

2. **训练配置 schema**
   - 文件：`src/ashare_ai/models/training_config.py`
   - 包含：
     ```python
     class TrainingConfig(BaseModel):
         model_type: str  # "scorer", "ranker", etc.
         training_data_path: str
         validation_split: float = 0.2
         hyperparameters: dict
         checkpoint_interval: int = 100
         max_epochs: int = 100
     ```

3. **添加训练 API 端点**
   - 在 `api/app.py` 添加：
     ```python
     @router.post("/api/v1/models/train")
     async def start_training(config: TrainingConfig):
         job_id = enqueue_training(config)
         return {"job_id": job_id, "status": "queued"}
     
     @router.get("/api/v1/models/train/{job_id}")
     async def get_training_status(job_id: str):
         return get_job_status(job_id)
     ```

### 5B.2 模型存储和版本管理

**设计原则**:
- 模型文件存储在持久卷
- 数据库记录模型元数据
- 支持模型版本切换

**实施步骤**:

1. **创建模型目录结构**
   ```
   /data/models/
   ├── scorer/
   │   ├── v1/
   │   │   ├── model.pkl
   │   │   └── metadata.json
   │   ├── v2/
   │   └── latest -> v2/
   ├── ranker/
   └── embeddings/
   ```

2. **数据库 schema**
   - 表：`model_versions`
   - 字段：
     ```sql
     CREATE TABLE model_versions (
         id SERIAL PRIMARY KEY,
         model_type VARCHAR(50) NOT NULL,
         version INTEGER NOT NULL,
         file_path TEXT NOT NULL,
         metrics JSONB,
         training_config JSONB,
         created_at TIMESTAMP DEFAULT NOW(),
         is_active BOOLEAN DEFAULT FALSE
     );
     ```

3. **模型加载器**
   - 文件：`src/ashare_ai/models/model_loader.py`
   - 功能：
     ```python
     class ModelLoader:
         def load_active_model(self, model_type: str):
             """Load currently active model."""
         
         def load_model_version(self, model_type: str, version: int):
             """Load specific model version."""
         
         def activate_model(self, model_id: int):
             """Switch active model to specified version."""
     ```

4. **模型卷配置**
   - 在 `compose.yaml` 添加：
     ```yaml
     volumes:
       model-data:
     
     services:
       api:
         volumes:
           - model-data:/data/models:ro  # 只读
       
       job-worker:
         volumes:
           - model-data:/data/models:rw  # 训练 worker 需要写入
     ```

### 5B.3 训练监控和日志

**实施步骤**:

1. **训练进度追踪**
   - 使用 Redis checkpoint：
     ```python
     def save_checkpoint(job_id: str, epoch: int, metrics: dict):
         redis.hset(
             f"ashare:training:{job_id}",
             mapping={
                 "epoch": epoch,
                 "loss": metrics["loss"],
                 "val_loss": metrics["val_loss"],
                 "progress": epoch / max_epochs,
             }
         )
     ```

2. **训练日志**
   - 输出到标准输出（Docker logs 捕获）
   - 关键指标存入数据库
   - 支持 TensorBoard (可选)

3. **训练通知**
   - 训练完成后发送通知
   - 验证指标低于阈值时发送警告

### 5B.4 评分模型集成

**前提**: 确认评分模块需要 ML 模型

**实施步骤**:

1. **重构评分模块**
   - 修改 `scoring/*.py`
   - 从 ModelLoader 加载模型
   - 保持现有 API 接口不变

2. **特征管道**
   - 确保特征工程输出与训练输入一致
   - 实现特征版本控制（如果需要）

3. **在线推理优化**
   - 批量推理
   - 模型缓存
   - 异步推理（如果需要）

## Phase 5C: 仅推理集成（方向 B）

如果系统使用预训练模型但不需要训练能力：

**实施步骤**:

1. **创建模型目录**
   ```bash
   mkdir -p models/
   # 将预训练模型文件放入此目录
   ```

2. **添加模型卷**
   ```yaml
   # compose.yaml
   volumes:
     model-data:
   
   services:
     api:
       volumes:
         - ./models:/data/models:ro
   ```

3. **实现模型加载器**
   - 仅实现加载功能，无训练功能
   - 启动时加载模型到内存

4. **集成到评分模块**
   - 使用加载的模型进行推理

## 交付物清单

### 方向 A (无需训练)
- `docs/phase5-model-training-plan.md` - 标记为 N/A
- `docs/fusion-progress.md` - 标记 Phase 5 跳过

### 方向 B (仅推理)
- `models/` - 模型文件目录
- `src/ashare_ai/models/model_loader.py` - 模型加载器
- `compose.yaml` - 添加 model-data 卷
- `docs/fusion-progress.md` - 标记 Phase 5 完成（简化版）

### 方向 C (完整训练)
- `src/ashare_ai/models/training_config.py` - 训练配置 schema
- `src/ashare_ai/models/model_loader.py` - 模型加载和版本管理
- `src/ashare_ai/orchestration/training_jobs.py` - 训练任务 handler
- `src/ashare_ai/orchestration/redis_queue.py` - 添加 model-training 队列
- `src/ashare_ai/api/app.py` - 添加训练 API 端点
- `compose.yaml` - 添加 model-data 卷
- `migrations/*.sql` - 添加 model_versions 表
- `tests/test_training_integration.py` - 训练集成测试
- `docs/fusion-progress.md` - 标记 Phase 5 完成

## 验收标准

### 方向 A (无需训练)
- [ ] 确认评分模块不依赖 ML 模型
- [ ] 文档说明为何跳过 Phase 5

### 方向 B (仅推理)
- [ ] 模型文件可以正常加载
- [ ] 评分模块使用加载的模型进行推理
- [ ] 模型文件持久化存储
- [ ] 容器重启后模型可用

### 方向 C (完整训练)
- [ ] 训练任务可以成功入队
- [ ] 训练 worker 可以执行训练任务
- [ ] 训练过程支持 checkpoint 恢复
- [ ] 训练完成后模型正确保存
- [ ] 模型版本管理正常工作
- [ ] 可以切换不同模型版本
- [ ] 评分模块使用最新激活的模型
- [ ] 训练 API 端点正常工作
- [ ] 训练监控和日志正常

## 风险和注意事项

### 技术风险

1. **训练时间过长**
   - 风险：阻塞 worker 队列
   - 缓解：使用专用训练 worker（profile-gated）

2. **内存占用**
   - 风险：训练任务 OOM
   - 缓解：增加 worker 内存限制，使用数据生成器

3. **模型文件过大**
   - 风险：镜像体积膨胀
   - 缓解：模型文件存储在卷中，不打包进镜像

### 业务风险

1. **模型性能下降**
   - 风险：新训练模型比旧模型差
   - 缓解：验证通过才激活，保留回滚能力

2. **训练数据质量**
   - 风险：错误数据导致模型失效
   - 缓解：训练前数据验证

## 实施步骤顺序

1. **Phase 5A: 审查现状**（1-2 小时）
   - 检查评分模块实现
   - 检查 ML 依赖
   - 确定方向 A/B/C

2. **根据方向执行**
   - 方向 A: 更新文档（10 分钟）
   - 方向 B: 模型加载集成（2-3 小时）
   - 方向 C: 完整训练流程（10-15 小时）

## 后续阶段预览

完成 Phase 5 后，进入 Phase 6: Rust 优化

---

更新时间：2026-08-26
状态：规划完成，待审查现状后决定方向
