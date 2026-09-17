from dify_plugin import Plugin, DifyPluginEnv

# OFD 文本导出需要拉起 JVM 进程，大文件耗时较长，这里放宽单次请求超时时间。
plugin = Plugin(DifyPluginEnv(MAX_REQUEST_TIMEOUT=300))

if __name__ == "__main__":
    plugin.run()
