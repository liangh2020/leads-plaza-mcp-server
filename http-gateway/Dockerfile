# Leads Plaza HTTP 网关（可选部署方式；默认走 systemd，见 deploy.sh）
FROM python:3.12-slim
WORKDIR /app
# 布局：/app/mcp_server（业务逻辑）+ /app/http-gateway/gateway.py
COPY ../mcp_server /app/mcp_server
COPY . /app/http-gateway
RUN pip install --no-cache-dir -r /app/http-gateway/requirements.txt
ENV LEADS_NETWORK=base
EXPOSE 8090
CMD ["python", "-m", "uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8090", "--app-dir", "/app/http-gateway"]
