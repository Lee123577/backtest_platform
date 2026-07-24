import os

import uvicorn

if __name__ == "__main__":
    # 生产加固：默认关闭自动重载(reload 是开发特性，会暴露重载端口且浪费资源)，
    # 绑定地址 / 端口均可经环境变量覆盖。公网部署务必配合防火墙或反代。
    host = os.getenv("BIND_HOST", "0.0.0.0")
    port = int(os.getenv("BIND_PORT", "8000"))
    reload = os.getenv("RELOAD", "0") == "1"
    uvicorn.run("app.main:app", host=host, port=port, reload=reload)
