项目基于PaddleOCR-VL-1.6实现票据信息的结构化抽取与回填
PaddleOCR-VL-1.6项目地址：https://aistudio.baidu.com/modelsdetail/49710/intro

## Docker 一键部署

1. 准备环境变量：`cp .env.example .env`，填入 `BAILIAN_API_KEY`（规则抽取需要；仅识别可留空）
2. 启动：`docker compose up -d --build`
3. 访问：`http://localhost:8000`（端口可用 `.env` 的 `BILL_PORT` 修改）

数据与模型缓存全部位于 `/qn-server-data`（容器同名挂载）：重建容器不丢数据、不重下模型。
全新机器（无 cosfs 挂载）上数据目录自动创建，OCR 模型在首次识别时自动下载。
