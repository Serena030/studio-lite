# Studio · 轻装版

一个很小的工单台。你写一张单，点一下，它就把这张单交给 AI 去做，做完把话带回来给你。

一共两个文件：一个后端，一个页面。没有别的。

---

#@ 一、你需要先有什么

1. 一台能开着不关的电脑或服务器（自己的电脑也行，关机了就停了）
2. 上面装了 Python（3.10 以上）
3. 装好了 Claude Code，并且在终端里敲 ```claude``` 能用

第 3 条是关键：这个工单台自己不会写代码，它只是替你把单子递给 Claude Code。

---

#@ 二、装起来（三条命令）

打开终端，一条一条敲：

```
git clone https://github.com/Serena030/studio-lite.git
cd studio-lite
pip install fastapi uvicorn
```

#@# 然后启动

```
python3 server.py
```

看到一行 ```Uvicorn running on http://127.0.0.1:8770``` 就是好了。

浏览器打开：**http://127.0.0.1:8770**

这个窗口不要关。关了服务就停了。想停就在这个窗口按 Ctrl+C。

---

#@ 三、怎么用

**开一张单**
最上面那个框里写「要做什么」，比如「把首页背景改成粉色」。
下面那个框可以多说几句，也可以不写。点「开一张」。

**让它去做**
点开这张单 → 点「交出去跑」。
状态会变成「在做」。这时候 Claude Code 正在你的电脑上干活。

**看结果**
做完了状态变成「交回来了」，单子底下会出现一段话，是它自己说的——改了什么、还剩什么。
满意就点「收工」。不满意就在底下打字告诉它哪里不对，再点一次「交出去跑」，它看得见你说的话。

**做砸了**
状态会变成「塌了」，旁边写着为什么。点「再跑一次」就行。

---

#@ 四、它在哪个文件夹干活

默认是在 studio-lite 这个文件夹里。你肯定不想让它改这里——你想让它改你自己的项目。

启动的时候这样写（把路径换成你项目的路径）：

```
STUDIO_WORKDIR=/Users/你的名字/你的项目 python3 server.py
```

Windows 上用 PowerShell 的话：

```
$env:STUDIO_WORKDIR="C:\Users\你的名字\你的项目"
python3 server.py
```

---

#@ 五、想换成别的 AI

默认用的是 ```claude -p```。想换别的，改这一个地方：

```
STUDIO_WORKER_CMD="codex exec" python3 server.py
```

只要那条命令满足两点就行：从标准输入读文字，把结果打印出来。

---

#@ 六、几个你可能会问的

**「自动派工」是什么？要不要开？**
默认是关着的，意思是：单子开好了就躺在那儿，你不点它就不动。
这样最省钱——不点就不消耗任何额度。
真想让新单自动开跑，启动时加 ```STUDIO_AUTO_DISPATCH=1```。建议先别开。

**手机上能打开吗？**
可以，但要多做两件事，顺序不能反：

```
STUDIO_TOKEN=你自己编一串密码 STUDIO_HOST=0.0.0.0 python3 server.py
```

然后手机浏览器打开 ```http://电脑的IP:8770/?token=你编的那串密码```

⚠️ 先设 STUDIO_TOKEN 再改 STUDIO_HOST。顺序反了等于把你的电脑敞开给整个网络。

**数据存在哪？**
文件夹里的 studio.db。想搬走就整个复制走。删了就全没了。

**它跑太久了怎么办？**
默认半小时就掐断。想改：```STUDIO_WORKER_TIMEOUT=3600```（单位是秒）。

---

#@ 七、坏了怎么查

| 现象 | 多半是 |
|---|---|
| 打开网页一片空白 | 服务没起来，回终端看有没有报错 |
| 点了跑，马上「塌了」，写着找不到命令 | ```claude``` 没装好，或者不在 PATH 里。终端里单独敲 ```claude``` 试试 |
| 一直「在做」不动 | 它真的在跑，长任务要等。超过半小时会自己掐断 |
| 手机打不开 | 检查 STUDIO_HOST 是不是 0.0.0.0，还有防火墙 |

---

#@ 八、所有能调的东西

| 变量 | 默认 | 干什么的 |
|---|---|---|
| STUDIO_PORT | 8770 | 端口，被占用了就换一个 |
| STUDIO_HOST | 127.0.0.1 | 只有本机能开。改 0.0.0.0 别人才能进 |
| STUDIO_DB | ./studio.db | 数据存哪 |
| STUDIO_WORKER_CMD | claude -p | 干活的那条命令 |
| STUDIO_WORKDIR | 当前文件夹 | 它在哪个项目里干活 |
| STUDIO_WORKER_TIMEOUT | 1800 | 跑多久算超时，单位秒 |
| STUDIO_TOKEN | 空 | 口令。放到公网上必须设 |
| STUDIO_AUTO_DISPATCH | 0 | 1 = 新单自动开跑 |

---

#@ 九、给会看代码的人

```
GET    /api/orders?status=
GET    /api/orders/{no}
POST   /api/orders          {title, body, who}
PATCH  /api/orders/{no}     {title, body, worker, status}
POST   /api/orders/{no}/msg {who, text}
POST   /api/orders/{no}/run
DELETE /api/orders/{no}
```

设了 STUDIO_TOKEN 的话，每个请求带 ```X-Token``` 头。

状态流转：inbox → queued → working → review → merged，中途出事落 failed。
