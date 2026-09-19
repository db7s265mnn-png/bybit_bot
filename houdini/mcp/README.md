# MCP для SideFX Houdini (локальный sidecar)

Cursor на **вашем** компьютере запускает `server.py` по stdio. Сервер поднимает или цепляется к **sidecar внутри hython/Houdini** на `127.0.0.1:18991`. Оттуда я могу выполнять `hou.*`, собирать процедурную скалу, cook, save, export.

Это не общий удалённый shell и не дырка в интернет. Слушаем только localhost, каждый запрос с токеном (`~/.cursor_houdini_mcp_token`).

## Чего этот облачный чат сделать не может

Этот агент крутится на чужой VM. Ваш `127.0.0.1` отсюда не виден, self-hosted worker не подключён. Команды в Houdini заработают, когда вы откроете **этот репозиторий в Cursor Desktop** на машине с Houdini (или поставите [self-hosted worker](https://cursor.com/docs/cloud-agent/self-hosted) на тот же ПК).

## Где это в Cursor (экран Customize → MCPs)

Левая колонка **Customize**, сверху чип **MCPs**, заголовок *Connect External Tools with MCP*, кнопки **+ New** и **Documentation** — это витрина **облачных** MCP (Linear, Figma, Notion). Houdini в Marketplace нет и в этом списке сам не появится. **Browse Marketplace не нужен.**

Локальный Houdini подключается так:

1. Откройте **приложение Cursor** (редактор кода), не только сайт агентов. File → Open Folder → этот репозиторий, ветка с файлом `.cursor/mcp.json`.
2. Снова Customize → MCPs. Если проект подхвачен, в списке будет сервер `houdini` из `mcp.json`.
3. Если список пустой — **+ New** и локальный / stdio сервер (не URL):
   - Name: `houdini`
   - Command: `python` (если нет в PATH — `py`)
   - Args: `-3` (только для `py`) и полный путь  
     `C:\путь\к\bybit_bot\houdini\mcp\server.py`
   - Env: `HOUDINI_MCP_HOST=127.0.0.1`, `HOUDINI_MCP_PORT=18991`
4. Если **+ New** просит только URL (`http://...`) — это форма для Linear/Figma. Закройте её: наш сервер не HTTP, а локальный процесс.

С этой же страницы на сайте агентов облако **не дотянется** до Houdini на вашем ПК: `127.0.0.1` там — это не ваш компьютер.

## Один раз на Windows

1. Установите SideFX Houdini. Если `hython` не в PATH — задайте `HFS`, например  
   `C:\Program Files\Side Effects Software\Houdini 20.5.445`
2. Файл `.cursor/mcp.json` уже в репозитории. Если Cursor не находит `python`, в нём поставьте `"command": "py"` и args `["-3", "${workspaceFolder}/houdini/mcp/server.py"]`.
3. Customize → MCPs → сервер `houdini` зелёный. Иначе Reload Window.
4. В чате редактора: «запусти Houdini и собери скалу».

Либо откройте Houdini сами и поставьте автостарт sidecar:

в Cursor: инструмент `houdini_install_autostart`  
или вручную скопируйте package JSON из вывода этого инструмента в  
`Documents\houdini20.5\packages\cursor_houdini_mcp.json`.

После этого достаточно открыть Houdini — sidecar сам сядет на порт 18991.

## Инструменты

| Tool | Зачем |
| --- | --- |
| `houdini_status` | Найти hython/Houdini, жив ли sidecar |
| `houdini_start` | `hython` (без GUI) или `houdini` (окно) |
| `houdini_exec` | Python внутри сессии, `hou` уже есть. Результат — в `_result` |
| `houdini_build_rock` | Сеть VDB→полигоны из этого репо |
| `houdini_cook` / `houdini_nodes` | Cook и дерево нод |
| `houdini_save_hip` / `houdini_export_geo` | Сохранить hip / геометрию |
| `houdini_set_parm` | Параметр ноды |
| `houdini_screenshot` | Снимок Scene Viewer, только GUI |
| `houdini_stop` | Погасить sidecar |
| `houdini_install_autostart` | Package + `123.py` |

## Почему не голый hrpyc

У SideFX `hrpyc` нет авторизации, и `hou.hipFile.load` через прокси часто ломается. Sidecar принимает JSON на localhost и гоняет `hou` на главном потоке GUI через `hdefereval`.

## Проверка без Houdini

```
python -m unittest houdini.mcp.tests.test_framing houdini.mcp.tests.test_sidecar_and_mcp
```

из корня репозитория, либо:

```
python -m unittest discover -s houdini/mcp/tests -p "test_*.py"
```
