with open(r"C:\Users\26381\Desktop\adb-first\api_server.py", encoding="utf-8") as f:
    c = f.read()

old_stop = '''@app.post("/api/tasks/stop")
async def stop_task():
    """停止当前任务"""
    task_state["running"] = False
    return {"status": "stopped"}'''

new_stop = '''@app.post("/api/tasks/stop")
async def stop_task():
    """停止当前任务 — 设置停止标志 + 打断手机当前操作"""
    task_state["running"] = False
    # 发送 back 键打断正在执行的 uiautomator2 操作
    try:
        import uiautomator2 as u2
        d = u2.connect("RFCXA0W194D")
        d.press("back")
    except:
        pass
    return {"status": "stopped"}'''

c = c.replace(old_stop, new_stop)

with open(r"C:\Users\26381\Desktop\adb-first\api_server.py", "w", encoding="utf-8") as f:
    f.write(c)

print("Stop endpoint now sends back key to interrupt")
