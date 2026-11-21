# Cover Circuit

Cover Circuit 是参加 Builders Buildathon 的 Mech Royale Python bot。它使用 Python 标准库，并沿用官方 starter kit 的 `mechfight.py` 处理 `mechfight/1` 消息。其他源码按本仓库的 MIT License 发布。

比赛根据存活名次计分。Bot 会提前规划进圈路线，在安全时捡武器、能量方块和补给，交战时横移并预瞄。在血量低或被围攻时，它会寻找地形掩护，停火给回血创造机会。

| 文件 | 内容 |
| --- | --- |
| `bot.py` | 缩圈、拾取、选升级、撤退与交战决策 |
| `arena.py` | 地形网格、建筑遮挡、有限步数的 A* 绕路 |
| `combat.py` | 机枪提前量、迫击炮弹道、激光和闪电判断 |
| `mechfight.py` | 官方 Python 协议 wrapper |
| `Dockerfile` | 评测入口，固定 Python 镜像摘要 |

## 本地测试

从交付包顶层解压 `mechfight-kit.tar.gz`。以下命令适用于 Windows PowerShell。macOS/Linux 可以把 `py -3` 换成 `python3`。

```powershell
tar -xzf mechfight-kit.tar.gz
cd mechfight-kit
py -3 check/check.py -- py -3 -B -u ../mech-royale-bot/bot.py
```

如果装了 Docker Desktop，从 `mechfight-kit` 目录继续运行下面的命令。比赛要求使用 Linux 容器。

```powershell
docker build --platform linux/amd64 -t cover-circuit ../mech-royale-bot
py -3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$($PWD.Path)/arena:/arena:ro" cover-circuit
```

将本文件夹里的文件放到公开 GitHub 仓库根目录，让赛事可以直接找到 `Dockerfile`。运行时评测器会把地图挂载到 `/arena`。然后到赛道页提交仓库 URL。

## 来源

- [比赛赛道与原始 starter kit](https://www.joinbuilders.org/buildathon/tracks/builders)
- [官方 mechfight/1 协议](https://www.joinbuilders.org/buildathon/tracks/builders/files/protocol.md?inline=1)
