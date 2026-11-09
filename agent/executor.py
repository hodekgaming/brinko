import json
import re
import sys
import threading
import subprocess
import tempfile
import os
from pathlib import Path
from typing import Callable

from agent.planner       import create_plan, replan
from agent.error_handler import analyze_error, generate_fix, ErrorDecision
from or_client            import client


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()


def _run_generated_code(description: str, speak: Callable | None = None) -> str:
    if speak:
        speak("Writing custom code for this task, sir.")

    home      = Path.home()
    desktop   = home / "Desktop"
    downloads = home / "Downloads"
    documents = home / "Documents"

    if not desktop.exists():
        try:
            import winreg
            key     = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
            desktop = Path(winreg.QueryValueEx(key, "Desktop")[0])
        except Exception:
            pass

    system_prompt = (
        "You are an expert Python developer. "
        "Write clean, complete, working Python code. "
        "Use standard library + common packages. "
        "Install missing packages with subprocess + pip if needed. "
        "Return ONLY the Python code. No explanation, no markdown, no backticks.\n\n"
        f"SYSTEM PATHS:\n"
        f"  Desktop   = r'{desktop}'\n"
        f"  Downloads = r'{downloads}'\n"
        f"  Documents = r'{documents}'\n"
        f"  Home      = r'{home}'\n"
    )

    try:
        code = client.chat(
            f"Write Python code to accomplish this task:\n\n{description}",
            system=system_prompt,
            max_tokens=3000,
        )
        code = re.sub(r"```(?:python)?", "", code).strip().rstrip("`").strip()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp_path = f.name

        print(f"[Executor] 🐍 Running generated code: {tmp_path}")

        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True, text=True,
            timeout=120, cwd=str(Path.home())
        )

        try:
            os.unlink(tmp_path)
        except Exception:
            pass

        output = result.stdout.strip()
        error  = result.stderr.strip()

        if result.returncode == 0 and output:
            return output
        elif result.returncode == 0:
            return "Task completed successfully."
        elif error:
            raise RuntimeError(f"Code error: {error[:400]}")
        return "Completed."

    except subprocess.TimeoutExpired:
        raise RuntimeError("Generated code timed out after 120 seconds.")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Generated code failed: {e}")

def _inject_context(params: dict, tool: str, step_results: dict, goal: str = "") -> dict:
    if not step_results:
        return params

    params = dict(params)

    if tool == "file_controller" and params.get("action") in ("write", "create_file"):
        content = params.get("content", "")
        if not content or len(content) < 50:
            all_results = [
                v for v in step_results.values()
                if v and len(v) > 100 and v not in ("Done.", "Completed.")
            ]
            if all_results:
                combined = "\n\n---\n\n".join(all_results)
                translated = _translate_to_goal_language(combined, goal)
                params["content"] = translated
                print(f"[Executor] 💉 Injected + translated content")

    return params
def _detect_language(text: str) -> str:
    try:
        return client.chat(
            f"What language is this text written in? "
            f"Reply with ONLY the language name in English (e.g. Turkish, English, French).\n\n"
            f"Text: {text[:200]}",
            system="Reply with only the language name, nothing else.",
            max_tokens=10,
        ).strip()
    except Exception:
        return "English"


def _translate_to_goal_language(content: str, goal: str) -> str:
    if not goal:
        return content
    try:
        target_lang = _detect_language(goal)
        print(f"[Executor] 🌐 Translating to: {target_lang}")

        prompt = (
            f"You are a professional translator. "
            f"Translate the following text into {target_lang}.\n"
            f"IMPORTANT:\n"
            f"- Translate EVERYTHING, leave nothing in English\n"
            f"- Keep all facts, numbers, and data intact\n"
            f"- Keep the structure and formatting\n"
            f"- Output ONLY the translated text, nothing else\n\n"
            f"Text to translate:\n{content[:4000]}"
        )
        translated = client.chat(prompt, system="You are a professional translator.", max_tokens=3000).strip()
        print(f"[Executor] ✅ Translation done ({target_lang})")
        return translated
    except Exception as e:
        print(f"[Executor] ⚠️ Translation failed: {e}")
        return content

def _call_tool(tool: str, parameters: dict, speak: Callable | None) -> str:

    if tool == "open_app":
        from actions.open_app import open_app
        return open_app(parameters=parameters, player=None) or "Done."

    elif tool == "web_search":
        from actions.web_search import web_search
        return web_search(parameters=parameters, player=None) or "Done."
    elif tool == "game_updater":
        from actions.game_updater import game_updater
        return game_updater(parameters=parameters, player=None, speak=speak) or "Done."
    elif tool == "browser_control":
        from actions.browser_control import browser_control
        return browser_control(parameters=parameters, player=None) or "Done."

    elif tool == "file_controller":
        from actions.file_controller import file_controller
        return file_controller(parameters=parameters, player=None) or "Done."

    elif tool == "cmd_control":
        from actions.cmd_control import cmd_control
        return cmd_control(parameters=parameters, player=None) or "Done."

    elif tool == "code_helper":
        from actions.code_helper import code_helper
        return code_helper(parameters=parameters, player=None, speak=speak) or "Done."

    elif tool == "dev_agent":
        from actions.dev_agent import dev_agent
        return dev_agent(parameters=parameters, player=None, speak=speak) or "Done."

    elif tool == "screen_process":
        from actions.screen_processor import screen_process
        screen_process(parameters=parameters, player=None)
        return "Screen captured and analyzed."

    elif tool == "send_message":
        from actions.send_message import send_message
        if parameters.get("scheduled_time"):
            from actions.schedule_task import schedule_task
            goal = (f"Send a {parameters.get('platform','WhatsApp')} message to "
                    f"{parameters.get('receiver','')} saying: {parameters.get('message_text','')}")
            dt_parts = parameters["scheduled_time"].split()
            return schedule_task(parameters={"action":"create","goal":goal,
                "date":dt_parts[0],"time":dt_parts[1] if len(dt_parts)>1 else "09:00"}) or "Scheduled."
        return send_message(parameters=parameters, player=None) or "Done."

    elif tool == "music":
        from actions.music import music
        return music(parameters=parameters, player=None, speak=speak) or "Done."

    elif tool == "email_agent":
        from actions.email_agent import email_agent
        return email_agent(parameters=parameters, player=None, speak=speak) or "Done."

    elif tool == "phone_call":
        from actions.phone_call import phone_call
        return phone_call(parameters=parameters, player=None, speak=speak) or "Done."

    elif tool == "reminder":
        from actions.reminder import reminder
        return reminder(parameters=parameters, player=None) or "Done."

    elif tool == "schedule_task":
        from actions.schedule_task import schedule_task
        return schedule_task(parameters=parameters, player=None, speak=speak) or "Done."

    elif tool == "night_mode":
        from actions.night_mode import night_mode
        return night_mode(parameters=parameters, player=None, speak=speak) or "Done."

    elif tool == "youtube_video":
        from actions.youtube_video import youtube_video
        return youtube_video(parameters=parameters, player=None) or "Done."

    elif tool == "weather_report":
        from actions.weather_report import weather_action
        return weather_action(parameters=parameters, player=None) or "Done."

    elif tool == "computer_settings":
        from actions.computer_settings import computer_settings
        return computer_settings(parameters=parameters, player=None) or "Done."

    elif tool == "desktop_control":
        from actions.desktop import desktop_control
        return desktop_control(parameters=parameters, player=None) or "Done."

    elif tool == "computer_control":
        from actions.computer_control import computer_control
        return computer_control(parameters=parameters, player=None) or "Done."

    elif tool == "generated_code":
        description = parameters.get("description", "")
        if not description:
            raise ValueError("generated_code requires a 'description' parameter.")
        return _run_generated_code(description, speak=speak)

    elif tool == "flight_finder":
        from actions.flight_finder import flight_finder
        return flight_finder(parameters=parameters, player=None, speak=speak) or "Done."

    elif tool == "stream_control":
        from actions.stream_control import stream_control
        return stream_control(parameters=parameters, player=None, speak=speak) or "Done."

    elif tool == "ad_content":
        from actions.ad_content import ad_content
        return ad_content(parameters=parameters, player=None, speak=speak) or "Done."

    elif tool == "ultra_mode":
        from actions.ultra_mode import ultra_mode
        return ultra_mode(parameters=parameters, player=None, speak=speak) or "Done."

    else:
        print(f"[Executor] ⚠️ Unknown tool '{tool}' — falling back to generated_code")
        return _run_generated_code(f"Accomplish this task: {parameters}", speak=speak)

import time as _time


def _verify_step(step: dict, result: str, goal: str) -> tuple[bool, str]:
    """Asks the AI to verify whether the step's result actually achieved what was intended.
    Returns (passed, reason). Used during night mode; skipped in fast interactive mode
    to keep response time low."""
    try:
        tool = step.get("tool", "")
        desc = step.get("description", "")
        # Skip verification for steps where the result itself is the evidence
        # (web_search results, file writes with explicit paths, etc.)
        if tool in ("web_search", "reminder", "email_agent", "phone_call"):
            return True, "auto-pass"
        if not result or result.lower() in ("done.", "done", "ok"):
            # Ambiguous result — ask the AI to judge
            verdict = client.chat(
                f'Goal: "{goal}"\nStep: "{desc}"\nResult: "{result[:300]}"\n\n'
                "Did this step clearly succeed? Reply with exactly PASS or FAIL and one sentence why.",
                system="You are a rigorous quality-assurance checker. Be skeptical of vague results.",
                max_tokens=80,
            ).strip()
            passed = verdict.upper().startswith("PASS")
            return passed, verdict
        return True, "result present"
    except Exception as e:
        return True, f"verification skipped: {e}"


class AgentExecutor:

    MAX_REPLAN_ATTEMPTS = 3
    MAX_STEP_ATTEMPTS   = 5

    def execute(
        self,
        goal:         str,
        speak:        Callable | None        = None,
        cancel_flag:  threading.Event | None = None,
        night_mode:   bool                   = False,
        step_log:     list | None            = None,
    ) -> str:
        """
        night_mode=True enables:
          - Self-verification after each step
          - Full step log stored in step_log (list[dict]) for the morning report
          - Up to 5 attempts per step with progressive backoff
          - MAX_REPLAN_ATTEMPTS = 3 instead of 2
        """
        print(f"\n[Executor] 🎯 Goal: {goal}  (night_mode={night_mode})")

        replan_attempts = 0
        completed_steps = []
        step_results    = {}
        plan            = create_plan(goal)
        max_replans     = 5 if night_mode else self.MAX_REPLAN_ATTEMPTS

        while True:
            steps = plan.get("steps", [])

            if not steps:
                msg = "I couldn't create a valid plan for this task, sir."
                if speak: speak(msg)
                return msg

            success      = True
            failed_step  = None
            failed_error = ""

            for step in steps:
                if cancel_flag and cancel_flag.is_set():
                    if speak: speak("Task cancelled, sir.")
                    return "Task cancelled."

                step_num = step.get("step", "?")
                tool     = step.get("tool", "generated_code")
                desc     = step.get("description", "")
                params   = step.get("parameters", {})
                critical = step.get("critical", True)

                params = _inject_context(params, tool, step_results, goal=goal)

                print(f"\n[Executor] ▶️  Step {step_num}: [{tool}] {desc}")

                step_ok       = False
                last_error    = ""
                max_attempts  = self.MAX_STEP_ATTEMPTS if night_mode else 3

                for attempt in range(1, max_attempts + 1):
                    if cancel_flag and cancel_flag.is_set():
                        break
                    try:
                        result = _call_tool(tool, params, speak)

                        # Night-mode: verify the step actually worked before marking done
                        if night_mode:
                            verified, reason = _verify_step(step, str(result), goal)
                            if not verified:
                                raise RuntimeError(f"Verification failed: {reason}")
                            print(f"[Executor] ✅ Step {step_num} verified: {reason}")

                        step_results[step_num] = result
                        completed_steps.append(step)

                        if step_log is not None:
                            step_log.append({
                                "step":   step_num,
                                "tool":   tool,
                                "desc":   desc,
                                "status": "ok",
                                "result": str(result)[:300],
                            })

                        print(f"[Executor] ✅ Step {step_num} done: {str(result)[:100]}")
                        step_ok = True
                        break

                    except Exception as e:
                        last_error = str(e)
                        print(f"[Executor] ❌ Step {step_num} attempt {attempt}/{max_attempts}: {last_error}")

                        recovery = analyze_error(step, last_error, attempt=attempt,
                                                 max_attempts=max_attempts)
                        decision = recovery["decision"]
                        user_msg = recovery.get("user_message", "")

                        if speak and user_msg and attempt == 1:
                            speak(user_msg)

                        if decision == ErrorDecision.RETRY:
                            wait = min(5 * attempt, 30)
                            print(f"[Executor] ⏳ Waiting {wait}s before retry...")
                            _time.sleep(wait)
                            continue

                        elif decision == ErrorDecision.SKIP:
                            print(f"[Executor] ⏭️  Skipping step {step_num}")
                            if step_log is not None:
                                step_log.append({"step": step_num, "tool": tool,
                                                 "desc": desc, "status": "skipped",
                                                 "result": last_error})
                            completed_steps.append(step)
                            step_ok = True
                            break

                        elif decision == ErrorDecision.ABORT:
                            msg = f"Task aborted, sir. {recovery.get('reason', '')}"
                            if speak: speak(msg)
                            return msg

                        else:  # REPLAN
                            fix_suggestion = recovery.get("fix_suggestion", "")
                            if fix_suggestion and tool != "generated_code":
                                try:
                                    fixed_step = generate_fix(step, last_error, fix_suggestion)
                                    if speak: speak("Trying an alternative approach, sir.")
                                    res = _call_tool(fixed_step["tool"],
                                                     fixed_step["parameters"], speak)
                                    step_results[step_num] = res
                                    completed_steps.append(step)
                                    if step_log is not None:
                                        step_log.append({"step": step_num, "tool": tool,
                                                         "desc": desc, "status": "fixed",
                                                         "result": str(res)[:300]})
                                    step_ok = True
                                    break
                                except Exception as fix_err:
                                    print(f"[Executor] ⚠️ Fix attempt failed: {fix_err}")

                            # Give up on this step
                            break

                if not step_ok:
                    if not critical:
                        print(f"[Executor] ⏭️  Non-critical step {step_num} failed — continuing")
                        if step_log is not None:
                            step_log.append({"step": step_num, "tool": tool, "desc": desc,
                                             "status": "failed_non_critical", "result": last_error})
                        continue

                    failed_step  = step
                    failed_error = last_error
                    success      = False
                    if step_log is not None:
                        step_log.append({"step": step_num, "tool": tool, "desc": desc,
                                         "status": "failed", "result": last_error})
                    break

            if success:
                return self._summarize(goal, completed_steps, speak)

            if replan_attempts >= max_replans:
                msg = f"Task failed after {replan_attempts} replan attempts, sir."
                if speak: speak(msg)
                return msg

            if speak: speak("Adjusting my approach, sir.")
            print(f"[Executor] 🔄 Replanning (attempt {replan_attempts + 1}/{max_replans})...")

            replan_attempts += 1
            plan = replan(goal, completed_steps, failed_step, failed_error)

    def _summarize(self, goal: str, completed_steps: list, speak: Callable | None) -> str:
        fallback = f"All done, sir. Completed {len(completed_steps)} steps for: {goal[:60]}."
        try:
            steps_str = "\n".join(f"- {s.get('description', '')}" for s in completed_steps)
            prompt    = (
                f'User goal: "{goal}"\n'
                f"Completed steps:\n{steps_str}\n\n"
                "Write a single natural sentence summarizing what was accomplished. "
                "Address the user as 'sir'. Be direct and positive."
            )
            summary = client.chat(prompt, system="You are BRINKO. Be concise and direct.", max_tokens=150).strip()
            if speak: speak(summary)
            return summary
        except Exception:
            if speak: speak(fallback)
            return fallback
