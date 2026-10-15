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
   like, then "+ Add to list" (or Enter). New tasks go to the end of their
   priority tag, so tasks you entered earlier stay above them.

   Each task has a **priority tag**: Low, Med or High (Med for a new task).
   Pick it with the Low / Med / High buttons in the task's editor. High and Low
   tasks show their tag at the start of the row, for example "High · 60m ·
   Email cleanup"; Med tasks show none.

3. **Your list** has two ways to sort, picked with "Sort" on the desktop and
   the "My order" / "By date" tabs on the phone:
   - **My order**, every task, dated or not: High tasks first, then Med, then
     Low, and inside each tag the order you put them in. On the desktop, drag
     a task by the ≡ sign at its left, or use the ↑ ↓ at its right. Pressing a
     task and dragging it moves it without opening it; a plain click opens it.
     On the phone, use ↑ ↓ or ⤒ (to the top of its tag). A task moves only
     among the tasks with the same tag; to move it to another group, change
     its tag.
   - **By date**, tasks with a deadline or a target finish date first, the
     closest date first, for example "Fri 10/16". The rest follow by tag and
     your order (on the phone they keep their ↑ ↓ ⤒ buttons there).

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
   | `P` or the calendar button | Open or close the Day plan window |
   | `M` or the minus button | Shrink to a small circle. One click on the circle expands it again (dragging it only moves it) |
   | `Esc` or the close button | Hide the widget. `Ctrl+Alt+F` or the tray icon brings it back |

   The All tasks window is a normal window. Use the Windows title bar buttons to
   minimize or maximize it, or press F11 for full screen (Esc leaves it). Enter or
   the menu button on the widget, or its taskbar button, brings it back as it was.

8. Drag the widget anywhere, on any monitor. It remembers where you left it.
   If that monitor is unplugged, it comes back on the primary screen.

## Dates: deadline and target

A task can have a **deadline** (a hard date) and a **target** (the "aim to
finish by" date). Set either or both in the editor; leave a date off and the
task follows its tag and your order instead.

- A **hard date** is a meeting or a scheduled event. The day plan books the
  task on that day, never earlier.
- An **aim date** is a date to finish by. The work can be done in advance, and
  the day plan spreads it over the days before.

The two kinds look different in the list, on the desktop, on the phone and in
the day plan. A hard deadline shows as "📅 Due Fri 10/16" in red, a target as
"🎯 Aim Mon 10/19" in the normal colour, and a task with both as
"📅 Due Fri 10/16, 🎯 aim Wed 10/14".

A deadline can also have a time of day, for example 2pm. Type it in the small
box next to the deadline date (2pm, 2:30pm and 14:00 all work), or leave it
empty if the task only has a date. The list then shows "📅 Due Fri 10/16
2:00pm", and the day plan books the task at exactly 2:00pm for its minutes,
even outside your working hours. A target date has no time, and neither does a
repeating task's deadline (it only says the last day it repeats).

The day plan (below) aims to finish aim-date tasks two business days (Monday
to Friday) before the date itself, so you are never racing the date. If a task
has both a deadline and a target, the deadline wins.

## Folders

Folders are labels, and can have subfolders, for example
"Garden/Seeds". Give a task a folder in the editor. Use the folder picker above
the list to show only one folder and its subfolders at a time, "No folder" to
see only the tasks without a folder, or "All folders" (on the desktop, "All
tasks") to see everything. There is one single order across all folders;
folders only filter what you see, they do not change the order itself.
Folders cannot have their own dates. Use "Folders..." to rename or delete a
folder; deleting one leaves its tasks without a folder, it does not delete the
tasks.

### Folder notes

When the list shows one folder, a "📝 Notes" box appears above its tasks (on
the desktop and on the phone). It has two parts:

- a **summary**, one free text box for where the project stands and what
  comes next. It saves by itself a moment after you stop typing (on the phone,
  when you leave the box).
- **progress notes**, short dated entries. Type one and press Enter (or Add)
  and it is stamped with today's date. They are listed newest first, and each
  one can be edited or deleted.

Click the "📝 Notes" line to fold the box away. Notes follow a folder when you
rename it. Deleting a folder keeps its notes, and they show again if you make
a folder with the same name. Progress notes written on the phone and on the
desktop are merged, so none are lost. If the same summary is changed on two
devices before they sync, the later change wins.

## Repeating tasks

A task can repeat: every day, on weekdays, on chosen days of the week, or
every so many days. A repeating task is one single item in your list, marked
with a repeat icon, and it only counts as active on the days it is due. On a
day it is not due, it is greyed out and cannot be started.

Pressing Done on a repeating task logs that day's copy to Completed and the
task goes back to sleep until its next due day. If you miss a day, it is just
skipped; nothing piles up or carries over.

A repeating task can also have a deadline. It then repeats on its days up to
and including the deadline, and the day after the deadline it leaves your list
on its own (on every device). A repeating task never has an "aim to finish by"
date.

Every repeating task shows the next day it comes up as its due date (today when
it is due today), in red, then the last day it repeats if it has one, for
example "📅 Due Wed 10/14 · 🔁 until Fri 10/30 · 5m · Water plants". In the By
date view it sits among your dated tasks by that next day.

## The day plan and the 7 pm job

Every evening around 7 pm (America/Chicago time), an automatic job looks at
your working hours, your dated tasks and your repeating tasks, and lays out a
plan for the next day, picking which task to work on, in which block of time,
and for how long. You can see this in the **Plan** tab on your phone, or in
the **Day plan** window on the desktop. Open that window with the calendar
button in the widget's hover buttons, the `P` key, or "Day plan..." in the
tray menu, and click the button again to close it. It shows warnings if
something will not fit before its date. Double-click a time block to start
its task. The window remembers its size and position on each computer.

The plan fills a day in this order:

1. Tasks with a hard date and a time, at exactly that time (a meeting at 14:00
   for 60 minutes is booked 14:00 to 15:00). If two of them overlap, both stay
   and the plan shows a warning.
2. Tasks with a hard date and no time, on that day, as early as they fit
   (High before Med before Low). If one does not fit on its day, the plan
   warns.
3. Overdue work: a hard date that has passed, and aim-date work that is too
   close to its date.
4. Repeating tasks due that day.
5. Aim-date work, pulled ahead into free time: High before Med before Low,
   then your order.
6. Tasks with no date: High before Med before Low, then your order.

For example, with the hours 9:00 to 12:00 and 13:30 to 18:00, a 60 minute
meeting at 14:00, a 30 minute form due today, a 120 minute Low report to aim
for by Friday, a 60 minute High "Email cleanup" with no date and a 60 minute
Med "Read paper" with no date give: form 9:00 to 9:30, report 9:30 to 11:30,
email 11:30 to 12:00 and 13:30 to 14:00, meeting 14:00 to 15:00, read 15:00
to 16:00.

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
   stored in Windows Credential Manager, and a copy goes in this computer's
   settings file (`%APPDATA%\FocusTool\local.json`), encrypted so only your
   Windows account can read it. Some computers drop the Credential Manager
   entry after a restart; the copy keeps the app connected. It is never put in
   this folder, in Box or on GitHub. If neither copy can be found, the panel
   says "GitHub key missing" in red.
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
