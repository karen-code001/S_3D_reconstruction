# 三维重建任务 Web 界面

这是一个无需构建工具的响应式用户端界面，覆盖三条核心流程：

1. 上传视频并展示后端返回的 `task_id`；
2. 输入 `task_id` 查询任务状态，支持每 3 秒自动刷新；
3. 输入 `task_id` 下载重建结果。

## 启动

在项目根目录运行：

```powershell
python -m http.server 5173 -d webui
```

然后访问 `http://localhost:5173`。也可以随控制节点启动后访问 `http://localhost:13000/ui/`（端口按实际启动参数调整）。

## 接口配置

页面右上角的“接口设置”可以修改 API 地址及接口路径。路径中的任务编号使用 `{task_id}` 占位符。

根据项目中现有路由扫描，当前初始值为：

- 创建任务：`/api/v1/tasks`（POST，JSON）
- 查询：`/api/v1/tasks/{task_id}`（GET）
- 结果查询：`/api/v1/tasks/{task_id}`（GET，读取 `result_url`）

上传遵循现有后端的两步流程：先创建任务并获取 `task_id` 与一次性 `upload_url`，再向该地址以 `file` 字段上传视频。

## 期望响应

上传响应至少应包含以下任一字段：`task_id`、`taskId`、`id`。查询响应可使用 `status` / `state` 和 `progress` / `percent`。下载接口既支持直接返回文件，也支持 JSON 中返回 `download_url` / `url` / `result_url`。

如前端与 API 不在同一域名，请在后端启用 CORS。生产环境建议由现有后端或 Nginx 直接托管本目录。
