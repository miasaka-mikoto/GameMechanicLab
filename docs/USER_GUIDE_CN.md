# Game Mechanic Lab 使用说明

Game Mechanic Lab（游戏机制与数值实验室）是一个本地、数据驱动的 2D 战斗数值实验工具。它不是完整游戏：每场战斗都会产生事件流，批量运行使用可复现的随机种子，结果可保存为实验记录。

## 启动

在项目根目录执行：

```bash
# 桌面 Arena（Windows 双击也可运行打包后的 GameMechanicLab.exe）
python app.py

# 完整 Demo：3 个 Build、3 个普通敌人、1 个三阶段 Boss，并生成扫参结果
python -m gamemechaniclab --demo --headless --runs 1 --output artifacts/demo
```

没有图形桌面时使用 `--headless`。`--runs` 支持 `1`、`100`、`10000`；大规模运行建议只保留一个样本回放，以免把所有事件写入内存。

不带 `--demo` 时，命令只跑一个真实的 Build vs Boss 对局批次，适合快速基准和 10,000 场压力测试；带 `--demo` 才会运行完整的 12 个场景与扫参。

```bash
python -m gamemechaniclab --headless --runs 10000
```

## 打包

Windows 上在 PowerShell 执行：

```powershell
.\build_windows.ps1 -Clean
```

生成 `dist\GameMechanicLab.exe`。Linux/macOS 开发机可执行 `./build_portable.sh` 生成便携目录；它不是 Windows `.exe`，Windows 包需在 Windows 环境构建。

## 桌面页面

- **Combat Arena**：选择 Build 和 Enemy，运行 1/100/10000 场，查看 HP、胜率、DPS、平均/中位数/p95 TTK（仅统计成功击杀）、承伤、失败数与实际玩家死亡数，以及事件 Feed；没有成功击杀时 TTK 为 `null`。
- **Experiments**：记录研究问题、假设、运行次数、种子和结论。
- **Builds / Skills / Enemies**：编辑角色属性、装备、被动、技能、敌人 AI、抗性和阶段。
- **Formula Lab**：修改安全算术公式并立即运行合成战斗。
- **Combat Replay**：按时间查看 AttackStarted、Hit、Damage、Critical、Dodge、SkillCast、Buff/Debuff、PhaseChanged 和 Death。
- **SWEEP**：对 Boss HP、攻击力等参数做笛卡尔积扫参，并查看胜率热图。

## 数据文件

可编辑数据位于 `configs/`，每类同时提供 JSON 和 YAML：

| 文件 | 内容 |
|---|---|
| `mechanics` | 机制组件、公式和状态效果 |
| `builds` | 3 个示例 Build（均衡、爆发、防御控制） |
| `skills` | 5 个玩家技能 |
| `enemies` | 3 个普通敌人 |
| `bosses` | 三阶段 Clockwork Tyrant |

数值通过 ID 引用，不写死在战斗循环中。改动后可运行 `pytest -q` 验证 JSON/YAML 一致性和引用结构。

`load_project()` 是统一的数据入口：技能 ID、`mechanics.status_effects` 中的状态
ID，以及 Boss 阶段的 `add_status`/`status_effects`，都会在构造模型前展开。装备和
被动可以用平坦修饰（如 `attack: 20`）或乘区修饰（如
`attack_multiplier: 1.2`）；Demo 中的 `Executioner.damage_below_half_hp` 会在目标
低于 50% HP 时作为条件伤害乘区参与模拟。

## 公式安全

公式只允许白名单变量、算术运算和有限数学函数，例如：

```text
max(0, attack * multiplier - defense)
```

系统会拒绝属性访问、导入、任意函数调用、列表推导、语句和过大的指数表达式；不会执行用户输入的任意 Python 代码。

也可以在无界面模式下直接比较多条公式。每个 `--formula-variant` 使用
`名称=公式`，可重复添加；系统会先统一校验，然后使用相同随机种子运行配对
实验：

```bash
python -m gamemechaniclab --headless --runs 100 \
  --formula-variant "baseline=attack * multiplier" \
  --formula-variant "armored=max(0, attack * multiplier - defense * 1.2)" \
  --output artifacts/formula_study
```

输出目录中的 `formula_comparison.json` 和 `formula_comparison.csv` 会记录每条
公式的胜率、DPS、平均/中位数/p95 TTK、承伤、死亡数，以及相对第一条公式的差值。若公式不安全，
会在模拟开始前拒绝，不会产生部分结果。

默认公式表示暴击前的基础伤害，模拟器随后应用实际暴击倍率；如果公式显式引用
`is_critical` 或 `critical_multiplier`，则由公式自行负责暴击缩放，系统不会再次相乘。

批量结果包含 `simulation_mode`：单场/回放和小批量为 `event_replay`；大批量且
关闭回放时为 `accelerated_approximation`。后者仍运行事件时钟、冷却、资源、暴击、
闪避、阶段、护盾和 DOT/HOT，但不保存连续位置轨迹，并在聚合路径即时结算投射物
飞行；敌方承伤暴露系数来自 `mechanics.simulation_defaults`，不是隐藏常数。
随附 Demo 使用 `fast_enemy_exposure: 1.0`，与完整事件路径的敌方出伤一致；
若为了校准近似模型而降低该值，应把它作为实验配置记录。

## Demo 输出

`--output artifacts/demo` 会生成：

- `demo_results.json`：场景和批量统计；
- `balance_sweep.csv`：Boss HP × Build 扫参表；
- `win_rate_heatmap.svg` / `win_rate_heatmap.json`：热图数据和矢量图；
- `ttk_heatmap.{json,png,svg}`：Boss HP × Build 的中位 TTK 热图；
- `replay_sample.json`：一场完整事件回放；
- `demo_experiment_record.json`、`demo_report.md`：可复现的实验记录和报告。
- `damage_vs_boss_hp_heatmap.{json,png,svg}`：玩家攻击力 × Boss HP 的中位 TTK 热图（包含低于基准的攻击力档位，便于看到过强/过弱边界）；同目录另有 `_win_rate` 版本。
- 同名 `.svg` 文件可用于矢量导出和文档排版。

## 常用检查

```bash
pytest -q
python -m compileall gamemechaniclab
python docs/capture_screenshots.py --output artifacts/screenshots
# 无图形桌面时，从真实 Demo 回放生成可复现截图
python docs/render_headless_screenshots.py --input artifacts/final_demo --output artifacts/screenshots
```

截图脚本需要可用的桌面显示（Linux 可在 `xvfb-run` 下执行）。

## 解释结果时的注意事项

Balance Warning 是可解释的风险信号，不是对真实玩家行为的证明。确认结论时应保留配置、公式、种子、运行次数和代表性回放，并把重要结论放回目标游戏实测。
