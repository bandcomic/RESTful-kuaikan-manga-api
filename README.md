# 快看漫画 API - EdgeOne Pages

基于 Python (Flask) 的快看漫画 RESTful API，部署在腾讯云 EdgeOne Pages。

## 特性

- 搜索漫画：中文关键词搜索
- 漫画详情：标题 / 封面 / 人气 / 标签 / 章节数
- 章节图片：`/v2/pweb/comic/inner/<id>` 公开 JSON 接口，无需解密
- 图片处理：缩放 / JPEG 质量 / PNG 量化 / LVGL 预解码
- Cookie 支持：转发请求方 Cookie，支持已购付费章节
- 图片由 API 直出（无开放代理），防 SSRF 滥用

## 项目结构

```
├── cloud-functions/
│   ├── index.py          # Flask 入口（路由 /）
│   └── [[default]].py    # Flask 入口（路由 /*），与 index.py 内容一致
├── requirements.txt
└── edgeone.json
```

> 注意：两个入口文件内容一致，修改代码时需保持同步。

## 本地开发

```bash
pip install -r requirements.txt
python cloud-functions/index.py        # http://localhost:8000
```

## EdgeOne Pages 部署

1. Fork 或克隆此仓库
2. 在 EdgeOne Makers 控制台导入 Git 仓库
3. 公网地址经 `Eo-Pages-Host` 请求头自动识别；若有异常可配置环境变量 `PUBLIC_URL=https://your-domain` 强制指定

## API

基础 URL：`https://your-project.edgeone.app`

| 端点 | 说明 |
| --- | --- |
| `GET /config` | 漫画源配置 |
| `GET /search/<keyword>/<page>` | 搜索漫画（上游单页全量返回，`has_more` 恒为 false） |
| `GET /comic/<topic_id>` | 漫画详情 |
| `GET /comic/<topic_id>/cover` | 漫画封面 |
| `GET /photo/<topic_id>/chapter/<chapter>` | 章节图片列表，`chapter` 从 1 开始 |
| `GET /photo/<topic_id>/chapter/<chapter>/<page>.jpg` | 单页图片，`page` 从 1 开始 |

图片参数（封面 / 单页通用）：`width`（目标宽度，仅缩小）、`quality`（1-100，默认 50）、`ifPNG=1`（返回 PNG）、`ifLVGL=1`（返回 LVGL 预解码二进制，仅正文，优先级高于 ifPNG）。

错误返回：`{ "code": 404, "message": "Comic not found" }`

## Cookie

免费章节无需登录；付费章节需购买后由请求方携带 Cookie 访问，服务端自动转发上游。带 Cookie 的请求不读写缓存。

## 实现说明

- 详情与章节列表：`/web/topic/<id>/` SSR 页面内嵌的 `window.__NUXT__` 状态（IIFE 字面量表达式，零依赖微型解析器求值）；章节图片：`/v2/pweb/comic/inner/<id>` JSON 接口；搜索：`/sou/<keyword>/` SSR 页面。
- 章节按 SSR 内嵌数组官方顺序编号 1..N（章节 id 非单调，不能按 id 排序）。
- 章节图片地址带签名（有效期约 1 小时）：章节缓存 30 分钟，图片请求失败时自动刷新重试一次。
- 缓存：详情 10 分钟、章节数据 30 分钟（模块级内存缓存，暖实例间共享）。

## 免责声明

本项目仅用于学习和研究目的，请勿用于商业用途。漫画内容版权归快看漫画及原作者所有，请支持正版。

## 许可证

MIT License
