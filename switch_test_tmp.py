import json, subprocess, time
from pathlib import Path
import handoff, supervisor, native_scheduler as ns, desktop
from account_pool import AccountPool, PoolConfig

WS = Path.home() / "omni-route-switch-test"
if WS.exists():
    subprocess.run(["rm", "-rf", str(WS)], check=False)
WS.mkdir(parents=True)
subprocess.run(["git", "init", "-q", str(WS)], check=True)
for k, v in (("user.email", "t@e.com"), ("user.name", "T")):
    subprocess.run(["git", "-C", str(WS), "config", k, v], check=True)
(WS / "README.md").write_text("switch test\n", encoding="utf-8")
subprocess.run(["git", "-C", str(WS), "add", "-A"], check=True)
subprocess.run(["git", "-C", str(WS), "commit", "-qm", "init"], check=True)

TOKEN = "switch-half-ok"
handoff.write(WS, handoff.Handoff(
    goal="Prove the switch half of rotation.",
    progress="Outgoing account hit its threshold and wrote this handoff.",
    decisions="none",
    files="SWITCH_PROOF.txt",
    status="no tests",
    blockers="none",
    next_action=f"Create a file named SWITCH_PROOF.txt in this workspace whose "
                f"entire contents are exactly: {TOKEN}\nThen stop.",
    from_account="claude-3", to_account="claude-1",
))
print("handoff armed in", WS, flush=True)

pool = AccountPool(PoolConfig.load())
current = next(a for a in pool.config.accounts if a.name == "claude-3")
sup = supervisor.Supervisor(WS, pool=pool)
print("rotating away from claude-3 ...", flush=True)
start = time.time()
result = sup.rotate(reason="switch-half test", exhausted=current)
print(f"\nrotation finished in {time.time()-start:.0f}s")
print(json.dumps(result.to_dict(), indent=2))

proof = WS / "SWITCH_PROOF.txt"
print("\nproof file:", proof.exists())
if proof.exists():
    print("contents  :", proof.read_text().strip())
