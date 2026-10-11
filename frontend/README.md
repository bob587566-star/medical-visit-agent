# 知禾 · 就诊准备前端

独立 Vue 3 / TypeScript 页面，仅使用本机字体和自绘 SVG。所有用户、资料及模型文本使用 Vue 插值渲染，来源链接仅允许 HTTP(S)。

```powershell
npm ci
npm run build
npm test
npm run test:e2e
```

开发：`npm run dev`，打开 `http://127.0.0.1:5174`。Vite 把 `/api` 代理到 `http://127.0.0.1:8270`；生产页面由后端直接服务 `dist/`，API 全部同源。首次启动从 `/api/bootstrap` 建立访客 Cookie / CSRF 会话，无记录时自动创建一份空白记录。

外部模型默认关闭，必须主动启用；服务未配置模型时开关禁用。切换记录或新建记录会重置外部模型授权。保存过程中允许继续编辑；较早的回包和 AI 完成后的刷新都不会覆盖新草稿。

`npm test` 验证输出安全与来源链接、空答复显示、同源 Cookie / CSRF、模型授权和表单保存竞态。

`npm run test:e2e` 使用独立无头 Edge 访问已启动的 `127.0.0.1:8270`。仅填写虚构病例，所有外部模型开关保持关闭，检查真实保存、问答、下载、打印样式、XSS、紧急卡及 1440/390/320px 页面；截图与导出材料保存到项目 `artifacts/`。
