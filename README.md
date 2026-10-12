# Focus Tool v1.5

A small always-on-top Windows widget that makes time visible: a pastel disk that drains as your current task's estimate runs out, a little mascot in the middle whose mood follows the time, a small elapsed counter, and the clock time your list will end at. Built for time blindness. Gentle by design, so it never blocks, locks, nags, starts anything on its own, or finishes a task for you.

## Daily use

1. Double-click the widget (or press Enter while it has focus, or use the tray icon) to open the task panel.

2. **Add a task** in the card at the top of the panel: type the name, click a time pill (5, 10, 15, 25, 30, 45, 60 or 90 minutes), write details if you like, then "+ Add to list" (or Enter).

3. **Your list** shows every task in order. Drag to reorder or use ↑ ↓. Click a task and the same card at the top switches to "Selected task", where you change its name, time (pills) and details as you type. "+ New task" switches the card back to adding.

4. Nothing starts by itself. The ▶ button starts the task you selected in the list (or asks which one to start), turns into ⏸ while it runs, and back to ▶ when paused. Space does the same on the widget.

   To switch tasks, select a different task in the panel and press "▶ Start selected". This works whether the current task is running or paused. The task you leave is paused and keeps the time it already used, and the list shows it as, for example, "⏸ 15m · send rejection (2 min used)". When you start it again, the disk continues where it stopped (13 minutes left in that example), and Done records the minutes from every stretch. Done on a paused task you never restarted records the minutes it already used.

5. The disk drains. At 50% and 90% it breathes peach for two seconds. At the end it breathes, plays one soft chime, turns lavender, and asks what you want: **+5**, **+15**, **Done**, or **keep going**. Nothing is finished for you. Until you answer, the timer keeps counting overtime with a thin ring, and the widget comes back if it was hidden. If the computer restarts while a task is over time, it asks again. The mascot goes 🌱 waiting, 🙂 plenty of time, 😌 past halfway, 👀 nearly out, 😅 overtime, 😴 paused, 🎉 for three seconds when you press Done, 🥳 when the list is empty.

6. **Done** works whether or not the task was started. It logs the task, moves it to the **Completed** card in the panel (with planned and actual minutes), and waits for you to start the next one. "Put back in list" undoes a Done. "Clear completed" empties the card but keeps `log.csv`.

7. Hover the widget for buttons, or use keys while it has focus:

   | Key | Action |
   |---|---|
   | `Space` or ▶/⏸ | Start, pause, resume |
   | `+` | Add 5 minutes to the current task |
   | `D` or ✓ | Done |
   | `T` or 💭 | Dump a thought without leaving the task |
   | `Enter` or ☰ | Open the task panel |
   | `M` or – | Shrink to a small circle. One click on the circle expands it again (dragging it only moves it) |
   | `Esc` or ✕ | Hide the widget. `Ctrl+Alt+F` or the tray icon brings it back |

8. **Dumped thoughts** appear at the bottom of the panel. Select one and press "→ Make it a task", or "Delete".

9. Drag the widget anywhere, on any monitor. It remembers where you left it. If that monitor is unplugged, it comes back on the primary screen.

## Where your data lives

Since v1.5 everything except the window position lives in the `data` folder next to this README, inside Box, so every computer that runs the app from this Box folder sees the same list (open it from the tray menu, "Open data folder (in Box)"):

- `settings.json` copied on first run from the computer's old settings, or from `settings.default.json`. Edit it to change the default block length, checkpoints, chime, toast, opacity, size, colours, hotkey, or turn the mascot off. Restart the tool after editing. Shared by all computers.
- `state.json` your remaining tasks, completed tasks and the running timer. It is written only when you change something (add, edit, reorder, start, pause, add time, Done), never on a schedule, so a computer that sits idle never writes an old list over a newer one. The timer is stored as a wall-clock start time, so it is still right after a restart, and on the other computer.
- `dump.txt` your captured thoughts, one per line with a timestamp. The panel reads and edits this file.
- `log.csv` one row per finished task: time, task, estimated minutes, actual minutes. Not shown anywhere yet; it is there for a later calibration view.

Each running copy looks at `state.json` every 2 seconds and shows changes the other computer saved, as soon as Box has brought them over (usually within seconds to a minute). If you change the list on both computers before Box has synced, the later save wins, and Box may keep the other one as a conflict copy in the same folder.

`%APPDATA%\FocusTool\` keeps what belongs to one computer only: `local.json` with the window position, panel size and whether the widget is shrunk. The files the app used before v1.5 (`state.json`, `dump.txt`, `log.csv`, `settings.json`) stay there untouched as a backup. The first time v1.5 starts on a computer it adds that computer's old tasks, completed tasks, thoughts and log rows to the shared folder, skipping any already there, and it does this only once.

## Running from source

```
%LOCALAPPDATA%\FocusTool\venv\Scripts\python.exe focus_tool.py
```

The virtual environment is created by `build.ps1` and deliberately lives outside Box.

## Building the exe

```
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

This runs the tests, then writes `build\FocusTool-YYYY-MM-DD-vN.exe`, choosing a new name every time so no earlier build is overwritten. The exe is unsigned, so on first launch Windows SmartScreen shows "Windows protected your PC". Click "More info", then "Run anyway". This repeats after every rebuild because the file hash changes.

## Start at login

Tick "Start at login" in the tray menu. It creates a shortcut in your Startup folder, which you can see and remove in Task Manager under Startup apps. No admin rights needed.

## Files

- `focus_core.py` all the logic (plan parser, timer, finish projection, reordering, saving, dump file access). No Qt.
- `tests/test_focus_core.py` tests for the logic, `tests/test_widget_buttons.py` tests that the widget's Done buttons and the one-click circle work, and `tests/test_sync.py` tests sharing the list between computers. Run with `python -m pytest -q tests`.
- `focus_tool.py` the widget, the task panel, tray icon, hotkey, chime, dialogs.
- `settings.default.json` defaults copied to your data folder on first run.
- `assets/` chime and icon.

## Not in this version (on purpose)

Reading flagged Outlook emails, TickTick sync, the estimate-vs-actual calibration view, any blocking of sites or apps, streaks or scores. Outlook (Classic desktop, via its automation interface) and the TickTick API were both checked and are feasible for a later version.
