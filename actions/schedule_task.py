# actions/schedule_task.py
#
# Schedules an arbitrary goal ("open Spotify and play my top song", "open
# ChatGPT and send it a message", "check my email and read me what's new")
# to actually run at a later time — not just a notification like
# reminder.py, but real execution. It reuses agent.executor.AgentExecutor
# (the same planner/executor pipeline behind agent_task) rather than
# reinventing task execution: the scheduled run writes a tiny standalone
# script that imports that executor and calls it, then Windows Task
# Scheduler runs that script at the requested time.
#
# Windows only for now (Task Scheduler), same honest limitation as
# reminder.py and game_updater's scheduling — macOS/Linux would need
# launchd/cron wired up separately.

import os
import re
import sys
import json
import subprocess
from datetime import datetime, timedelta

_TASK_PREFIX = "BRINKO_ScheduledTask_"


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _python_exe() -> str:
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        pythonw = exe.replace("python.exe", "pythonw.exe")
        if os.path.exists(pythonw):
            return pythonw
    return exe


def _safe_task_name(when: datetime) -> str:
    return f"{_TASK_PREFIX}{when.strftime('%Y%m%d_%H%M%S')}"


def _build_run_script(goal: str, temp_dir: str, task_name: str) -> str:
    """A minimal standalone script — no GUI, no voice session — that just
    runs the goal through the same executor agent_task already uses, then
    cleans itself up."""
    escaped_goal = goal.replace('"""', '\\"\\"\\"')
    script_path = os.path.join(temp_dir, f"{task_name}.py")
    script_code = f'''import sys
sys.path.insert(0, r"{_project_root()}")

goal = """{escaped_goal}"""

try:
    from agent.executor import AgentExecutor
    AgentExecutor().execute(goal=goal, speak=None, cancel_flag=None)
except Exception as e:
    try:
        import traceback
        with open(r"{temp_dir}\\\\{task_name}_error.log", "w", encoding="utf-8") as f:
            f.write(f"Scheduled task failed: {{e}}\\n")
            traceback.print_exc(file=f)
    except Exception:
        pass

try:
    import os as _os
    _os.remove(__file__)
except Exception:
    pass
'''
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_code)
    return script_path


def _build_xml(task_name: str, description: str, python_exe: str, script_path: str,
                start_dt: datetime, recurrence: str | None) -> str:
    trigger = f'''<TimeTrigger>
      <StartBoundary>{start_dt.strftime("%Y-%m-%dT%H:%M:%S")}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>'''

    if recurrence == "daily":
        trigger = f'''<CalendarTrigger>
      <StartBoundary>{start_dt.strftime("%Y-%m-%dT%H:%M:%S")}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>'''
    elif recurrence == "weekly":
        day_name = start_dt.strftime("%A")
        trigger = f'''<CalendarTrigger>
      <StartBoundary>{start_dt.strftime("%Y-%m-%dT%H:%M:%S")}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek><{day_name}/></DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>'''

    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>BRINKO scheduled task: {description}</Description>
  </RegistrationInfo>
  <Triggers>
    {trigger}
  </Triggers>
  <Actions>
    <Exec>
      <Command>{python_exe}</Command>
      <Arguments>"{script_path}"</Arguments>
    </Exec>
  </Actions>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Principals>
    <Principal>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
</Task>'''


def _create(goal: str, date_str: str, time_str: str, recurrence: str | None) -> str:
    try:
        target_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        return "I couldn't understand that date or time format, sir — use YYYY-MM-DD and HH:MM."

    if recurrence not in (None, "", "daily", "weekly"):
        return "Recurrence must be 'daily', 'weekly', or left out for one-time, sir."

    if not recurrence and target_dt <= datetime.now():
        return "That time is already in the past, sir."

    task_name  = _safe_task_name(target_dt)
    temp_dir   = os.environ.get("TEMP", "C:\\Temp")
    python_exe = _python_exe()

    script_path = _build_run_script(goal, temp_dir, task_name)
    xml_content = _build_xml(task_name, goal, python_exe, script_path, target_dt, recurrence or None)

    xml_path = os.path.join(temp_dir, f"{task_name}.xml")
    with open(xml_path, "w", encoding="utf-16") as f:
        f.write(xml_content)

    result = subprocess.run(
        f'schtasks /Create /TN "{task_name}" /XML "{xml_path}" /F',
        shell=True, capture_output=True, text=True
    )

    try:
        os.remove(xml_path)
    except Exception:
        pass

    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        print(f"[ScheduleTask] ❌ schtasks failed: {err}")
        try:
            os.remove(script_path)
        except Exception:
            pass
        return f"I couldn't schedule that, sir: {err[:150] or 'unknown scheduler error'}"

    when_str = target_dt.strftime("%B %d at %I:%M %p")
    freq = {"daily": " (repeating daily)", "weekly": " (repeating weekly)"}.get(recurrence, "")
    return f"Scheduled for {when_str}{freq}: {goal}"


def _list() -> str:
    result = subprocess.run(
        f'schtasks /Query /FO CSV /NH',
        shell=True, capture_output=True, text=True
    )
    if result.returncode != 0:
        return "Couldn't read the task list, sir."

    tasks = []
    for line in result.stdout.splitlines():
        if _TASK_PREFIX in line:
            fields = [f.strip('"') for f in line.split('","')]
            if fields:
                name = fields[0].lstrip("\\")
                tasks.append(name)

    if not tasks:
        return "No scheduled tasks right now, sir."
    return f"{len(tasks)} scheduled task(s):\n" + "\n".join(f"  - {t}" for t in tasks)


def _cancel(task_hint: str) -> str:
    result = subprocess.run('schtasks /Query /FO CSV /NH', shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        return "Couldn't read the task list, sir."

    matches = []
    for line in result.stdout.splitlines():
        if _TASK_PREFIX in line:
            fields = [f.strip('"') for f in line.split('","')]
            if fields:
                name = fields[0].lstrip("\\")
                if not task_hint or task_hint.lower() in name.lower():
                    matches.append(name)

    if not matches:
        return "Couldn't find a matching scheduled task, sir."

    cancelled = []
    for name in matches:
        r = subprocess.run(f'schtasks /Delete /TN "{name}" /F', shell=True, capture_output=True, text=True)
        if r.returncode == 0:
            cancelled.append(name)

    return f"Cancelled {len(cancelled)} task(s), sir." if cancelled else "Couldn't cancel that task, sir."


def schedule_task(
    parameters: dict,
    response=None,
    player=None,
    speak=None,
    session_memory=None,
) -> str:
    """
    parameters:
        action     : create | list | cancel  (default: create)
        goal       : what to do — anything agent_task could handle
                     (e.g. "open Spotify and play my top song")
        date       : YYYY-MM-DD
        time       : HH:MM (24h)
        recurrence : "daily" | "weekly" (optional — omit for one-time)
        task_hint  : for cancel — a fragment to match against scheduled tasks
    """
    if sys.platform != "win32":
        return ("Scheduled tasks currently only work on Windows (they use Task "
                "Scheduler). macOS/Linux would need launchd/cron wired up separately.")

    p      = parameters or {}
    action = p.get("action", "create").lower().strip()

    if player:
        player.write_log(f"[Schedule] {action}")

    if action == "list":
        return _list()

    if action == "cancel":
        return _cancel(p.get("task_hint", ""))

    goal = p.get("goal", "").strip()
    date_str = p.get("date", "").strip()
    time_str = p.get("time", "").strip()

    if not goal:
        return "What should I do when it runs, sir?"
    if not date_str or not time_str:
        return "I need both a date and a time, sir."

    return _create(goal, date_str, time_str, p.get("recurrence", "").strip().lower() or None)
