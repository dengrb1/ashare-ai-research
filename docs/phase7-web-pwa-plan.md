# Phase 7: Web/PWA 增强

## 目标

评估前端功能完整性，决定是否需要增强 Web UI 或添加 PWA 能力。

## 前置条件

- 已完成 Phase 1-6
- 后端 API 功能完整
- 基本 Web UI 可用

## Phase 7A: 前端现状审查

### 实施步骤

1. **审查前端技术栈**
   ```bash
   cd web
   cat package.json | grep -E "react|vue|angular|svelte"
   cat package.json | grep -E "vite|webpack|next"
   ```

2. **审查现有功能**
   ```bash
   # 查看路由定义
   grep -r "Route\|path:" web/src/
   
   # 查看页面组件
   find web/src -name "*Page.tsx" -o -name "*Page.jsx"
   
   # 查看主要功能模块
   ls -la web/src/features/ web/src/pages/
   ```

3. **审查构建配置**
   ```bash
   cat web/vite.config.ts  # 或 webpack.config.js
   cat web/tsconfig.json
   ```

4. **审查 PWA 现状**
   ```bash
   # 检查是否已有 PWA 配置
   ls web/public/manifest.json
   ls web/src/service-worker.js
   grep -r "workbox\|sw\.js" web/
   ```

### 审查维度

1. **功能完整性**
   - [ ] 研究任务管理（创建、查看、状态）
   - [ ] 研究报告查看（图表、数据表）
   - [ ] 回测任务管理
   - [ ] 回测结果展示（收益曲线、回撤、指标）
   - [ ] 评分结果查看
   - [ ] 交易计划查看（模拟）
   - [ ] 用户认证（登录、注册）
   - [ ] 系统设置
   - [ ] 健康监控

2. **用户体验**
   - [ ] 响应式设计（移动端适配）
   - [ ] 加载状态提示
   - [ ] 错误处理和提示
   - [ ] 数据可视化质量
   - [ ] 交互流畅度

3. **PWA 能力**
   - [ ] 离线访问
   - [ ] 可安装（Add to Home Screen）
   - [ ] 推送通知
   - [ ] 后台同步

### 决策点

根据审查结果：

**方向 A**: 前端功能完整，用户体验良好，无 PWA 需求
- → **跳过 Phase 7**，仅标记完成

**方向 B**: 功能完整，但需要 UI/UX 优化
- → **Phase 7B**: UI/UX 优化

**方向 C**: 功能不完整，缺少关键页面
- → **Phase 7C**: 功能补齐

**方向 D**: 需要 PWA 能力（离线访问、推送）
- → **Phase 7D**: PWA 改造

## Phase 7B: UI/UX 优化（方向 B）

### 实施步骤

1. **响应式设计优化**
   - 审查移动端布局
   - 使用 Tailwind/CSS Grid 优化响应式
   - 测试主要断点：320px, 768px, 1024px, 1440px

2. **数据可视化增强**
   ```bash
   # 检查当前图表库
   grep -E "recharts|chart\.js|d3|plotly|echarts" web/package.json
   ```
   
   可能需要：
   - 统一图表主题
   - 添加交互（缩放、tooltip、legend 交互）
   - 支持深色模式
   - 导出图表（PNG、SVG）

3. **性能优化**
   - 代码分割（React.lazy, Suspense）
   - 图片懒加载
   - 虚拟滚动（长列表）
   - Memoization（React.memo, useMemo）

4. **加载状态和错误处理**
   - 统一 Loading 组件
   - 统一 ErrorBoundary
   - Skeleton screens
   - Toast 通知

5. **深色模式**
   ```typescript
   // 检查是否已实现
   grep -r "dark.*mode\|theme.*toggle" web/src/
   ```
   
   如果未实现：
   - 添加主题切换
   - CSS 变量或 Tailwind dark:
   - 持久化用户偏好

### 交付物

- 优化后的组件
- 性能测试报告
- 移动端测试截图

## Phase 7C: 功能补齐（方向 C）

### 可能缺失的功能

1. **研究报告详情页**
   - 完整报告展示
   - 图表可视化
   - 数据表格
   - 导出 PDF（可选）

2. **回测详情页**
   - 收益曲线
   - 回撤图
   - 交易记录表
   - 指标卡片（年化收益、夏普比率等）

3. **任务监控页面**
   - 任务队列状态
   - Worker 健康状态
   - 系统指标（内存、CPU）

4. **个人归档**
   - 归档任务列表
   - 归档详情查看

### 实施步骤

对每个缺失功能：
1. 设计页面布局（Figma/手绘草图）
2. 实现 React 组件
3. 集成 API 调用
4. 添加加载和错误状态
5. 单元测试（可选）

### 交付物

- 新增页面组件
- 路由配置更新
- API 集成代码
- 功能演示截图

## Phase 7D: PWA 改造（方向 D）

### 前提条件
- 用户明确需要离线访问或移动端安装

### 7D.1 PWA 基础配置

**实施步骤**:

1. **添加 Web App Manifest**
   ```json
   // web/public/manifest.json
   {
     "name": "AShare AI Research Platform",
     "short_name": "AShare AI",
     "description": "股票研究和回测平台",
     "start_url": "/",
     "display": "standalone",
     "background_color": "#0F131C",
     "theme_color": "#38BDF8",
     "icons": [
       {
         "src": "/icons/icon-192.png",
         "sizes": "192x192",
         "type": "image/png"
       },
       {
         "src": "/icons/icon-512.png",
         "sizes": "512x512",
         "type": "image/png"
       }
     ]
   }
   ```

2. **生成 PWA 图标**
   ```bash
   # 使用工具生成各尺寸图标
   # https://realfavicongenerator.net/
   # 或 pwa-asset-generator
   npx pwa-asset-generator logo.svg ./public/icons
   ```

3. **HTML 引用 Manifest**
   ```html
   <!-- web/index.html -->
   <link rel="manifest" href="/manifest.json">
   <meta name="theme-color" content="#38BDF8">
   <link rel="apple-touch-icon" href="/icons/icon-192.png">
   ```

### 7D.2 Service Worker 配置

**方案 1: Vite PWA Plugin (推荐)**

```bash
npm install vite-plugin-pwa workbox-window
```

```typescript
// web/vite.config.ts
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.ico', 'robots.txt', 'icons/*.png'],
      manifest: {
        // ... (已在 public/manifest.json)
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff2}'],
        runtimeCaching: [
          {
            urlPattern: /^https:\/\/api\./,
            handler: 'NetworkFirst',
            options: {
              cacheName: 'api-cache',
              expiration: {
                maxEntries: 50,
                maxAgeSeconds: 5 * 60, // 5 分钟
              },
              cacheableResponse: {
                statuses: [0, 200],
              },
            },
          },
        ],
      },
    }),
  ],
})
```

**方案 2: 手写 Service Worker**

```javascript
// web/public/sw.js
const CACHE_NAME = 'ashare-ai-v1'
const urlsToCache = [
  '/',
  '/index.html',
  '/assets/index.css',
  '/assets/index.js',
]

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(urlsToCache))
  )
})

self.addEventListener('fetch', (event) => {
  event.respondWith(
    caches.match(event.request).then((response) => {
      return response || fetch(event.request)
    })
  )
})
```

### 7D.3 离线体验优化

**实施步骤**:

1. **离线提示**
   ```typescript
   // web/src/hooks/useOnlineStatus.ts
   import { useEffect, useState } from 'react'
   
   export function useOnlineStatus() {
     const [isOnline, setIsOnline] = useState(navigator.onLine)
   
     useEffect(() => {
       const handleOnline = () => setIsOnline(true)
       const handleOffline = () => setIsOnline(false)
   
       window.addEventListener('online', handleOnline)
       window.addEventListener('offline', handleOffline)
   
       return () => {
         window.removeEventListener('online', handleOnline)
         window.removeEventListener('offline', handleOffline)
       }
     }, [])
   
     return isOnline
   }
   ```

2. **离线 UI**
   ```typescript
   // web/src/components/OfflineBanner.tsx
   export function OfflineBanner() {
     const isOnline = useOnlineStatus()
   
     if (isOnline) return null
   
     return (
       <div className="fixed top-0 left-0 right-0 bg-amber-500 text-white p-2 text-center z-50">
         您当前处于离线状态，部分功能可能不可用
       </div>
     )
   }
   ```

3. **缓存策略**
   - 静态资源: Cache First
   - API 数据: Network First
   - 图片: Cache First with fallback

### 7D.4 推送通知（可选）

**前提**: 需要后端支持 Web Push

**实施步骤**:

1. **前端订阅**
   ```typescript
   // web/src/utils/push.ts
   export async function subscribeToPush() {
     if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
       console.warn('Push notifications not supported')
       return null
     }
   
     const registration = await navigator.serviceWorker.ready
     const subscription = await registration.pushManager.subscribe({
       userVisibleOnly: true,
       applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC_KEY),
     })
   
     // 发送订阅信息到后端
     await fetch('/api/v1/push/subscribe', {
       method: 'POST',
       headers: { 'Content-Type': 'application/json' },
       body: JSON.stringify(subscription),
     })
   
     return subscription
   }
   ```

2. **后端推送端点**
   ```python
   # src/ashare_ai/api/app.py
   @router.post("/api/v1/push/subscribe")
   async def subscribe_push(subscription: PushSubscription):
       # 存储订阅信息到数据库
       # ...
       return {"status": "subscribed"}
   
   @router.post("/api/v1/push/send")
   async def send_push_notification(user_id: str, message: str):
       # 发送推送到用户订阅端点
       # 使用 pywebpush 库
       # ...
   ```

3. **Service Worker 处理推送**
   ```javascript
   // web/public/sw.js
   self.addEventListener('push', (event) => {
     const data = event.data.json()
     const options = {
       body: data.body,
       icon: '/icons/icon-192.png',
       badge: '/icons/badge.png',
       data: { url: data.url },
     }
   
     event.waitUntil(
       self.registration.showNotification(data.title, options)
     )
   })
   
   self.addEventListener('notificationclick', (event) => {
     event.notification.close()
     event.waitUntil(
       clients.openWindow(event.notification.data.url)
     )
   })
   ```

### 7D.5 后台同步（可选）

**使用场景**: 离线时创建任务，上线后自动同步

```typescript
// web/src/utils/backgroundSync.ts
export async function scheduleBackgroundSync(tag: string, data: any) {
  if (!('serviceWorker' in navigator) || !('sync' in ServiceWorkerRegistration.prototype)) {
    console.warn('Background sync not supported')
    // 直接执行或存入 IndexedDB 待手动同步
    return
  }

  // 存储待同步数据
  await storeInIndexedDB(tag, data)

  const registration = await navigator.serviceWorker.ready
  await registration.sync.register(tag)
}
```

```javascript
// web/public/sw.js
self.addEventListener('sync', (event) => {
  if (event.tag === 'sync-research-tasks') {
    event.waitUntil(syncResearchTasks())
  }
})

async function syncResearchTasks() {
  const tasks = await getFromIndexedDB('pending-research-tasks')
  for (const task of tasks) {
    await fetch('/api/v1/research/runs', {
      method: 'POST',
      body: JSON.stringify(task),
    })
  }
  await clearFromIndexedDB('pending-research-tasks')
}
```

## Phase 7E: 移动端优化（可选）

### 实施步骤

1. **触摸优化**
   - 增大可点击区域（min 44x44px）
   - 手势支持（滑动、捏合）
   - 避免 hover 状态

2. **性能优化**
   - 图片优化（WebP、懒加载）
   - 减少 JS bundle 大小
   - 首屏渲染优化

3. **移动端特定功能**
   - 分享到社交媒体
   - 截图保存
   - 原生感觉的过渡动画

## 交付物清单

### Phase 7A (审查)
- `docs/phase7-web-pwa-plan.md` (本文件) - 审查报告和决策
- 前端功能清单
- 截图或录屏

### Phase 7B (UI/UX 优化)
- 优化后的组件代码
- 性能对比报告
- 移动端测试报告

### Phase 7C (功能补齐)
- 新增页面组件
- 路由更新
- API 集成代码

### Phase 7D (PWA)
- `web/public/manifest.json` - PWA Manifest
- `web/public/icons/*` - PWA 图标
- `web/vite.config.ts` - Vite PWA 插件配置
- `web/src/hooks/useOnlineStatus.ts` - 离线状态 hook
- `web/src/components/OfflineBanner.tsx` - 离线提示
- `web/src/utils/push.ts` - 推送通知工具（可选）
- `web/src/utils/backgroundSync.ts` - 后台同步（可选）
- `src/ashare_ai/api/app.py` - 推送端点（可选）

### 文档
- `docs/fusion-progress.md` - 标记 Phase 7 完成

## 验收标准

### Phase 7A
- [ ] 前端功能完整性审查完成
- [ ] 用户体验评估完成
- [ ] PWA 需求明确
- [ ] 已决定执行方向

### Phase 7B (UI/UX)
- [ ] 移动端响应式良好
- [ ] 图表交互流畅
- [ ] 深色模式（如果实现）正常
- [ ] 性能指标达标（LCP < 2.5s, FID < 100ms）

### Phase 7C (功能补齐)
- [ ] 所有关键页面实现完整
- [ ] API 集成正常
- [ ] 加载和错误状态处理正确

### Phase 7D (PWA)
- [ ] Lighthouse PWA 评分 > 90
- [ ] 可以 Add to Home Screen
- [ ] 离线时显示缓存内容
- [ ] Service Worker 注册成功
- [ ] 推送通知（如果实现）正常工作
- [ ] 后台同步（如果实现）正常工作

## 性能目标

### 核心 Web Vitals
- LCP (Largest Contentful Paint): < 2.5s
- FID (First Input Delay): < 100ms
- CLS (Cumulative Layout Shift): < 0.1

### PWA 特定
- 离线可用性: 关键页面离线可访问
- 安装提示: 访问 2 次后显示安装提示
- 启动速度: < 1s (从 Home Screen 启动)

### Bundle 大小
- Initial bundle: < 300KB (gzipped)
- Total JS: < 1MB (gzipped)
- 代码分割: 按路由懒加载

## 风险和注意事项

### 技术风险

1. **浏览器兼容性**
   - 风险: Service Worker 不支持 IE11
   - 缓解: 渐进增强，非 PWA 浏览器降级

2. **缓存一致性**
   - 风险: 用户看到过期内容
   - 缓解: 版本控制，主动更新提示

3. **推送通知权限**
   - 风险: 用户拒绝通知权限
   - 缓解: 优雅降级，非侵入式请求

### 业务风险

1. **离线功能限制**
   - 风险: 用户期望所有功能离线可用
   - 缓解: 明确告知哪些功能离线可用

2. **存储限制**
   - 风险: IndexedDB/Cache 配额不足
   - 缓解: 定期清理，优先缓存关键资源

## 实施步骤顺序

1. **Phase 7A: 审查**（2-3 小时）
   - 功能清单
   - 用户体验评估
   - 决策方向

2. **Phase 7B: UI/UX 优化**（8-15 小时）
   - 响应式优化
   - 图表增强
   - 性能优化

3. **Phase 7C: 功能补齐**（视缺失功能而定，5-20 小时）

4. **Phase 7D: PWA 改造**（8-12 小时）
   - PWA 基础配置（2 小时）
   - Service Worker（3 小时）
   - 离线体验（2 小时）
   - 推送通知（3 小时，可选）
   - 后台同步（2 小时，可选）

**预计总时间**:
- 仅审查: 2-3 小时
- 审查 + UI 优化: 10-18 小时
- 审查 + 功能补齐: 7-23 小时
- 审查 + PWA: 10-15 小时
- 全部: 20-40 小时

## 后续阶段预览

完成 Phase 7 后，进入 Phase 8: 离线部署包

---

更新时间：2026-08-26
状态：规划完成，待前端审查
