"""古诗词查询 MCP Server。

提供三个工具：search_poetry（按关键词搜索）、random_poetry（随机返回一首）、
list_poets（列出所有诗人，去重）。

运行方式（stdio 传输，供 MCP 客户端接入）：
    python mcp_servers/poetry_server.py
"""

# 作为一个 MCP Server，通过 stdio 暴露下面 3 个工具给客户端（客户端一侧见
# agent/tools/mcp.py）。工具在 Agent 侧会带服务名前缀 poetry-server__xxx，
# 避免不同 Server 的同名工具冲突。数据源为内存中的静态诗词，无文件/网络副作用。

import random

from mcp.server.fastmcp import FastMCP

# 硬编码诗词数据源：唐诗 + 宋词（纯内存静态数据，不落盘、不联网）
POEMS = [
    {"title": "静夜思", "author": "李白", "dynasty": "唐",
     "content": "床前明月光，疑是地上霜。举头望明月，低头思故乡。"},
    {"title": "春晓", "author": "孟浩然", "dynasty": "唐",
     "content": "春眠不觉晓，处处闻啼鸟。夜来风雨声，花落知多少。"},
    {"title": "登鹳雀楼", "author": "王之涣", "dynasty": "唐",
     "content": "白日依山尽，黄河入海流。欲穷千里目，更上一层楼。"},
    {"title": "望庐山瀑布", "author": "李白", "dynasty": "唐",
     "content": "日照香炉生紫烟，遥看瀑布挂前川。飞流直下三千尺，疑是银河落九天。"},
    {"title": "江雪", "author": "柳宗元", "dynasty": "唐",
     "content": "千山鸟飞绝，万径人踪灭。孤舟蓑笠翁，独钓寒江雪。"},
    {"title": "相思", "author": "王维", "dynasty": "唐",
     "content": "红豆生南国，春来发几枝。愿君多采撷，此物最相思。"},
    {"title": "悯农", "author": "李绅", "dynasty": "唐",
     "content": "锄禾日当午，汗滴禾下土。谁知盘中餐，粒粒皆辛苦。"},
    {"title": "早发白帝城", "author": "李白", "dynasty": "唐",
     "content": "朝辞白帝彩云间，千里江陵一日还。两岸猿声啼不住，轻舟已过万重山。"},
    {"title": "水调歌头·明月几时有", "author": "苏轼", "dynasty": "宋",
     "content": "明月几时有？把酒问青天。不知天上宫阙，今夕是何年。"
               "我欲乘风归去，又恐琼楼玉宇，高处不胜寒。起舞弄清影，何似在人间。"
               "转朱阁，低绮户，照无眠。不应有恨，何事长向别时圆？"
               "人有悲欢离合，月有阴晴圆缺，此事古难全。但愿人长久，千里共婵娟。"},
    {"title": "念奴娇·赤壁怀古", "author": "苏轼", "dynasty": "宋",
     "content": "大江东去，浪淘尽，千古风流人物。故垒西边，人道是，三国周郎赤壁。"
               "乱石穿空，惊涛拍岸，卷起千堆雪。江山如画，一时多少豪杰。"
               "遥想公瑾当年，小乔初嫁了，雄姿英发。羽扇纶巾，谈笑间，樯橹灰飞烟灭。"
               "故国神游，多情应笑我，早生华发。人生如梦，一尊还酹江月。"},
    {"title": "如梦令·常记溪亭日暮", "author": "李清照", "dynasty": "宋",
     "content": "常记溪亭日暮，沉醉不知归路。兴尽晚回舟，误入藕花深处。"
               "争渡，争渡，惊起一滩鸥鹭。"},
    {"title": "满江红·写怀", "author": "岳飞", "dynasty": "宋",
     "content": "怒发冲冠，凭栏处、潇潇雨歇。抬望眼，仰天长啸，壮怀激烈。"
               "三十功名尘与土，八千里路云和月。莫等闲，白了少年头，空悲切。"
               "靖康耻，犹未雪。臣子恨，何时灭。驾长车，踏破贺兰山缺。"
               "壮志饥餐胡虏肉，笑谈渴饮匈奴血。待从头、收拾旧山河，朝天阙。"},
]

# 服务名会体现在客户端工具名前缀（poetry-server__xxx），
# 同时也是 mcp.py 配置里用来索引该连接的 key。
mcp = FastMCP("poetry-server")


def _format_poem(poem: dict) -> str:
    """把一首诗格式化为可读文本。"""
    # 三个工具共用的内部辅助（未加 @mcp.tool()，不会被暴露成远程工具）
    return f"《{poem['title']}》—— {poem['author']}（{poem['dynasty']}）\n{poem['content']}"


# 装饰器把函数注册成一个可被 MCP 客户端调用的工具：参数名/类型注解自动生成
# 入参 JSON Schema，函数 docstring 作为工具 description 暴露给模型。
@mcp.tool()
def search_poetry(keyword: str) -> str:
    """在诗词标题和内容中搜索包含指定关键词的诗词。"""
    results = [
        _format_poem(p)
        for p in POEMS
        if keyword in p["title"] or keyword in p["content"]
    ]
    if not results:
        return "未找到包含该关键词的诗词"
    return "\n\n".join(results)


@mcp.tool()
def random_poetry() -> str:
    """随机返回一首诗词。"""
    return _format_poem(random.choice(POEMS))


@mcp.tool()
def list_poets() -> str:
    """返回所有诗人列表（去重）。"""
    poets = list(dict.fromkeys(p["author"] for p in POEMS))
    return "\n".join(poets)


if __name__ == "__main__":
    mcp.run(transport="stdio")
