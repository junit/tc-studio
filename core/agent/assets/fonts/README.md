# TC Studio PDF 中文字体

`TCStudioReportSansSC-Regular.ttf` 随应用分发，PDF 生成时嵌入实际使用的字形子集。
运行期无需访问网络或读取宿主系统字体；macOS、Linux 和 Docker 使用同一字体。

来源：Noto Sans SC，notofonts/noto-cjk 的固定提交
`f8d157532fbfaeda587e826d4cd5b21a49186f7c`。

- 源文件：<https://raw.githubusercontent.com/notofonts/noto-cjk/f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/Variable/TTF/Subset/NotoSansSC-VF.ttf>
- 源 SHA-256：`d68bafcb48a2707749396aa12bbbd833cb70401f3a9a689fd2902c7e0d295964`
- 分发 SHA-256：`d658b6ee175ea26d9e1157221c223423cd05f52897bdd286a5cb75858a2ffc92`
- Copyright: © 2014-2021 Adobe (http://www.adobe.com/), with Reserved Font Name 'Source'.
- 许可：同目录 `OFL.txt`，SIL Open Font License 1.1。

构建时使用 FontTools 4.63.0 的 `instantiateVariableFont(font, {"wght": 400}, inplace=True)`
冻结常规字重，删除可变字体表；保留全部字形。为区分派生字体，将 name 表的
1/2/4/6 项分别设为 `TC Studio Report Sans SC`、`Regular`、
`TC Studio Report Sans SC Regular`、`TCStudioReportSansSC-Regular`，
设置 Windows `(3, 1, 0x409)` 与 Macintosh `(1, 0, 0)` 两种记录，保留原版权与许可记录。
FontTools 仅用于维护期生成该静态资源，不是生产运行依赖。
