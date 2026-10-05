# 三平台部署

业务核心cloud-functions/backend.py；平台根/catch-all薄入口与Vercel/Gunicorn
根index.py共用核心。EdgeOne配置已修正为python.maxDuration。
保留官方章节顺序（不按ID排序）、锁定章节和SSR解析错误；图片列表改为稳定章节ID，
旧序号路径兼容，签名失效在合并任务内刷新一次。搜索/元数据/成品按身份缓存。

VPS：Python3.14 venv安装requirements，加载 `.env.example` 对应 `.env`，
`gunicorn -c gunicorn.conf.py index:app`。Vercel根index.py/Python3.12；EdgeOne
Python云函数，辅助文件就在产物目录。PUBLIC_URL可含路径前缀。

VPS设置持久共享CACHE_DB；Serverless留空仅暖实例协调。SQLite原子事务、TTL/
容量淘汰及90秒所有者租约，可选REDIS_URL跨实例共享，协调失效503，Redis用noeviction。
默认2worker×2thread、冷图单worker1/全局2；内存64MiB/worker、磁盘内容512MiB，
实际磁盘还含WAL/空闲页。下载20MiB、解码3200万像素、响应4MiB，环境变量可配。

图片width/w、quality/q，LVGL>PNG>JPEG；白底透明、LVGL宽高≤2047、完整内容/长度。
公共图24h，鉴权private,no-store，错误no-store；CDN鉴权跳过读写并保留全部参数。
Nginx转发127.0.0.1:3006，传Host和X-Forwarded-Host/Proto，读超时25秒。
诊断 `/health/runtime`、X-Cache、尺寸头。实际云平台与Vela/AstroBox需上线验收。
