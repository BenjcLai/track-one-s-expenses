from fastapi import FastAPI, Request

app = FastAPI()


@app.post("/webhook")
async def webhook(request: Request):
  body = await request.json()
  print("收到 LINE 請求：", body)
  return {"status": "ok"}