# Focus Tool v2

A small always-on-top Windows widget that makes time visible: a pastel disk that
drains as your current task's estimate runs out, a little mascot in the middle
whose mood follows the time, a small elapsed counter, and the clock time your
list will end at. Built for time blindness. Gentle by design, so it never
blocks, locks, nags, starts anything on its own, or finishes a task for you.

There is also a phone web app that shows and edits the same list, and an
automatic day plan that lays your tasks out against your working hours.

## Daily use (desktop)

1. Double-click the widget (or press Enter while it has focus, or use the tray
   icon) to open the task panel.

2. **Add a task** in the card at the top of the panel: type the name, click a
   time pill (5, 10, 15, 25, 30, 45, 60 or 90 minutes), write details if you
   like, then "+ Add to list" (or Enter). New tasks go to the front of the
   list, so what you just added is the next thing you will work on.

3. **Your list** is split into two parts:
   - **By date**, tasks with a deadline or a target finish date, shown with
     the closest date first, for example "Fri 10/16".
   - **Your order**, everything else, in the order you put it in. Drag to
     reorder, or use the up/down buttons, or "to top".

   Click a task and the same card at the top switches to "Selected task",
   where you change its name, time (pills), details, dates, folder or repeat
   setting as you go. "+ New task" switches the card back to adding.

4. Nothing starts by itself. The Start button starts the task you selected in
   the list (or asks which one to start), turns into Pause while it runs, and
   back to Start when paused. Space does the same on the widget.

   To switch tasks, select a different task in the panel and press "Start
   selected". This works whether the current task is running or paused. The
   task you leave is paused and keeps the time it already used, and the list
   shows it as, for example, "paused, 15m, Water plants (2 min used)". When
   you start it again, the disk continues where it stopped (13 minutes left in
   that example), and Done records the minutes from every stretch. Done on a
   paused task you never restarted records the minutes it already used.

5. The disk drains. At 50% and 90% it breathes peach for two seconds. At the
   end it breathes, plays one soft chime, turns lavender, and asks what you
   want: **+5**, **+15**, **Done**, or **keep going**. Nothing is finished for
   you. Until you answer, the timer keeps counting overtime with a thin ring,
   and the widget comes back if it was hidden. If the computer restarts while
   a task is over time, it asks again. The mascot goes from waiting, to plenty
   of time, to past halfway, to nearly out, to overtime, to paused; it
   celebrates for three seconds when you press Done, and again when the list
   is empty.

6. **Done** works whether or not the task was started. It logs the task, moves
   it to the **Completed** card in the panel (with planned and actual
   minutes), and waits for you to start the next one. "Put back in list"
   undoes a Done and puts the task back at the front. "Clear completed" empties
   the card but keeps `log.csv`.

   Done on a repeating task (see below) keeps the task in your list; only that
   day's copy moves to Completed.

7. Hover the widget for buttons, or use keys while it has focus:

   | Key | Action |
   |---|---|
   | `Space` or the play/pause button | Start, pause, resume |
   | `+` | Add 5 minutes to the current task |
   | `D` or the check button | Done |
   | `Enter` or the menu button | Open the task panel |
   | `M` or the minus button | Shrink to a small circle. One click on the circle expands it again (dragging it only moves it) |
   | `Esc` or the close button | Hide the widget. `Ctrl+Alt+F` or the tray icon brings it back |

8. Drag the widget anywhere, on any monitor. It remembers where you left it.
   If that monitor is unplugged, it comes back on the primary screen.

## Dates: deadline and target

A task can have a **deadline** (a hard date it must be done by) and a
**target** (the date you expect to finish it by, softer than a deadline). Set
either or both in the editor; leave a date off and the task follows your
manual order instead.

The day plan (below) finishes dated tasks two business days (Monday to
Friday) before the date itself, so you are never racing the actual deadline.
If a task has both a deadline and a target, the deadline wins.

## Folders

Folders are labels, and can have subfolders, for example
"Garden/Seeds". Give a task a folder in the editor. Use the folder picker above
the list to show only one folder and its subfolders at a time, or "All
folders" to see everything. There is one single order across all folders;
folders only filter what you see, they do not change the order itself.
Folders cannot have their own dates. Use "Folders..." to rename or delete a
folder; deleting one leaves its tasks without a folder, it does not delete the
tasks.

## Repeating tasks

A task can repeat: every day, on weekdays, on chosen days of the week, or
every so many days. A repeating task is one single item in your list, marked
with a repeat icon, and it only counts as active on the days it is due. On a
day it is not due, it is greyed out and cannot be started.

Pressing Done on a repeating task logs that day's copy to Completed and the
task goes back to sleep until its next due day. If you miss a day, it is just
skipped; nothing piles up or carries over.

## The day plan and the 7 pm job

Every evening around 7 pm (America/Chicago time), an automatic job looks at
your working hours, your dated tasks and your repeating tasks, and lays out a
plan for the next day, picking which task to work on, in which block of time,
and for how long. You can see this in the **Plan** tab on your phone, or in
the Plan card on the desktop panel. It shows warnings if something will not
fit before its date.

You do not have to wait for the evening job. A **Re-plan now** button is
available on both the phone and the desktop. Before 7 pm it plans the rest of
today starting from right now, and after 7 pm it plans tomorrow.

Your working hours are Monday to Friday 9:00 to 12:00 and 13:30 to 18:00, and
Saturday and Sunday 10:00 to 12:30, unless you change them in Settings.

## The phone web app

The same list and plan are available on your phone as a web app, so you do not
need to install anything from an app store.

1. Open the web app's address in Safari (or Chrome) on your phone.
2. Tap the Share button, then "Add to Home Screen".
3. Open it from the icon it creates. It then behaves like a normal app: full
   screen, its own icon, and it keeps working for a short time if you lose
   signal.

On an iPhone, open the app from the Home Screen icon first and enter the
token there (step 4 of the GitHub setup below). The Home Screen app keeps its
own storage, separate from Safari, so a token typed into a Safari tab is not
seen by the Home Screen app. Safari can also clear what a website stored if
you have not opened it for 7 days, so a Safari tab is not a safe place to keep
the token.

The phone app and the desktop app share the same list through GitHub (see
below), so a change on one shows up on the other within a few seconds, once
both are online.

## Setting up GitHub sync

Your list can be shared between your phone and your desktop through a private
GitHub repository, so the data stays yours and does not pass through any other
service. This is optional. If you never set it up, the desktop app keeps
working exactly as before, saving to the `data` folder inside Box.

1. Create a **private** GitHub repository to hold your data, for example
   named `focus-data`. Keep it private; it will hold your real tasks.
2. On GitHub, go to Settings, then "Developer settings", then "Personal
   access tokens", then "Fine-grained tokens", and create a new token:
   - Give it a name you will recognize later, such as "Focus Tool".
   - Set "Repository access" to "Only select repositories" and pick the
     `focus-data` repository you just created. Do not give it access to any
     other repository.
   - Under "Repository permissions", set "Contents" to "Read and write", so it
     can read and save your list.
   - Also set "Actions" to "Read and write", so the "Re-plan now" button can
     trigger the planning job.
   - Generate the token and copy it. GitHub shows it only once.
3. On the desktop, open the tray menu and choose "GitHub sync...". Enter the
   repository owner (your GitHub username), the repository name
   (`focus-data`), and paste the token, then "Test" and "Save". The token is
   stored in Windows Credential Manager, not in any file in this folder.
4. On the phone web app (opened from its Home Screen icon on an iPhone), open
   Settings, enter the same owner, repository and token, then "Test
   connection". The token is stored only in the browser on your phone (never
   sent anywhere except to GitHub).

   All GitHub Pages sites of one GitHub account share one browser origin
   (`yourname.github.io`), so any other Pages site on the same account could
   read what this web app stores, including the token. Do not host other
   GitHub Pages sites on this account, or give this one its own custom
   subdomain. The token itself can only reach the one private `focus-data`
   repository, so even a leaked token cannot touch anything else you own.
5. Once both are connected, GitHub becomes the master copy. Box's
   `data\state.json` keeps receiving a write-only backup copy from the
   desktop, so you always have a local file even if GitHub is unreachable,
   but it is only a backup once GitHub sync is on.

Your task names and details are never part of the public `focus-tool` code
repository. They live only in your private `focus-data` repository, in your
Box folder, and on your phone. A safety check (`tools/check_public.py`) runs
before every commit and before every build specifically to make sure nothing
from your real list ever ends up in the public repository by accident. The
build also runs it with `--history`, which scans every file version and
commit message in the whole git history, since a name that was committed once
and deleted later still becomes public when the history is pushed.

The check runs before every commit only once its git hook is installed. Do
this once on each computer, from this folder:

```
%LOCALAPPDATA%\FocusTool\venv\Scripts\python.exe tools\install_hooks.py
```

It copies `tools/pre-commit` into the hooks folder that
`git rev-parse --git-path hooks` names, which works even though this repo
keeps its git folder outside Box.

## Where your data lives

- `data\settings.json`, copied on first run from the computer's old settings,
  or from `settings.default.json`. Edit it to change the default block length,
  checkpoints, chime, toast, opacity, size, colours, hotkey, or turn the
  mascot off. Restart the tool after editing. Shared by all computers using
  Box.
- `data\state.json`, your remaining tasks, completed tasks, folders and the
  running timer. It is written only when you change something (add, edit,
  reorder, start, pause, add time, Done), never on a schedule, so a computer
  that sits idle never writes an old list over a newer one. In GitHub mode
  this file is a write-only backup; the live copy lives in your private
  `focus-data` repository instead.
- `data\day-plan.json`, the current day plan, written by the planner.
- `data\log.csv`, one row per finished task: time, task, estimated minutes,
  actual minutes. Not shown anywhere yet; it is there for a later calibration
  view.

Each running copy of the desktop app, while in Box mode, looks at
`state.json` every 2 seconds and shows changes the other computer saved, as
soon as Box has brought them over (usually within seconds to a minute). If you
change the list on both computers, the app merges the two lists when it saves,
task by task, so the edits from both computers are kept. If both computers
save at the very same moment, before Box has brought either change over, Box
may still keep one of the files as a conflict copy in the same folder. In
GitHub mode, syncing happens over the network instead, within a few seconds.

`%APPDATA%\FocusTool\` keeps what belongs to one computer only: `local.json`
with the window position, panel size and whether the widget is shrunk, and
`github-cache.json` with sync bookkeeping for GitHub mode. Neither file is
ever committed to any repository.

## Running from source

```
%LOCALAPPDATA%\FocusTool\venv\Scripts\python.exe focus_tool.py
```

The virtual environment is created by `build.ps1` and deliberately lives
outside Box.

## Building the exe

```
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

This installs dependencies, runs the Python tests and, if Node.js is
installed, the JavaScript tests for the web app's shared logic, then checks
that nothing unsafe for a public repository is about to be bundled, then
writes `build\FocusTool-YYYY-MM-DD-vN.exe`, choosing a new name every time so
no earlier build is overwritten. The exe is unsigned, so on first launch
Windows SmartScreen shows "Windows protected your PC". Click "More info", then
"Run anyway". This repeats after every rebuild because the file hash changes.

## Start at login

Tick "Start at login" in the tray menu. It creates a shortcut in your Startup
folder, which you can see and remove in Task Manager under Startup apps. No
admin rights needed.

## Files

- `focus_core.py`, the shared logic (tasks, timer, dates, folders, repeats,
  merging, saving). No Qt.
- `focus_tool.py`, the desktop widget, the task panel, tray icon, hotkey,
  chime, dialogs.
- `planner.py`, builds the day plan from your tasks and working hours.
- `gh_store.py`, talks to GitHub for sync, from the desktop app.
- `docs/`, the phone web app (plain HTML, CSS and JavaScript, no build step),
  published separately through GitHub Pages.
- `ops/focus-data/`, the files meant to live in your private `focus-data`
  repository: the nightly planning job and its own README.
- `tools/check_public.py`, the safety check described above,
  `tools/pre-commit`, a git hook that runs it automatically before each
  commit, and `tools/install_hooks.py`, which installs that hook.
- `tests/`, automated tests for all of the above. Run the Python tests with
  `python -m pytest -q tests`, and the JavaScript tests with
  `node --test tests/js`.
- `settings.default.json`, defaults copied to your data folder on first run.
- `assets/`, chime and icon.

## Not in this version (on purpose)

Reading flagged Outlook emails, the estimate-vs-actual calibration view, any
blocking of sites or apps, streaks or scores. Outlook (Classic desktop, via
its automation interface) was checked and is feasible for a later version.
