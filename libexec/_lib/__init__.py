"""BidGenie 脚本共享库（_lib）。

模块：
- errors.py  错误码 / 退出码 / 统一 fail() JSON / CLI 入口
- paths.py   工作区路径解析、目录命名、slug 化、时间戳
- jsonio.py  原子写、旁路文件锁、JSON 读写重试
- schema.py  PRD 第 7 章字段校验（bidder/project/state/…）与默认值归一化
- logging.py 事件日志 append-only JSONL 与敏感值掩码
- state.py   状态机唯一写者（迁移表/guard/effects/history/revision/registry 镜像）
- deps.py    插件自有依赖目录 plugin/pylibs 的 sys.path / DLL 路径挂载
- runtime.py DSH 运行时外部命令发现（node：BG_NODE → PATH → 运行时解释器兄弟目录）
- text.py    统一字数口径（CJK 字符 + ASCII 词），审查/合并/导出/概览共用
"""
