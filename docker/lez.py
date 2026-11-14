#!/usr/bin/env python3
"""The LEZ side of an RLN run, around logos-lez-rln's tools.

  lez.py init    before the sequencer: mint the payer that pays the deployment's
                 fees and write the sequencer config that funds it at genesis
  lez.py setup   once the sequencer answers: deploy an RLN registry, publish its
                 id, then fund every account a node asks for

Everything shared goes through /lez (out/lez on the host):
  seq/sequencer_config.json  the sequencer's config            (init)
  registry-id                logos:local:<hex>, what a node's RLN preset names (setup)
  fund/<node>                written by a node: the LEZ account to fund
  funded/<node>              written here: "ok", or the error

The recipe is logos-delivery-module's tests/e2e (conftest.py lez_registry).
"""
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request

LEZ = "/lez"
HOME = f"{LEZ}/home"
TOOLS = "/opt/lez-rln"
SEQUENCER = os.environ.get("LEZ_RLN_SEQUENCER", "http://lez-sequencer:3040/")
# LEZ's CLOCK_50 account and its clock shard, with the timestamp in ms at bytes
# 8-16: 0 until block 50, and the registry refuses registrations until then.
CLOCK_50 = "4BdcjoXkq786TMWcBGGHqcxeLYMZmn17rL4eM9ZyRWkX"
CLOCK_SHARD = "2hwaRW8sMnyKLLRbDRw8k68tiv9bnfq98oxcdtcp157Z"
GENESIS_BALANCE = 10**13
FUNDING = int(os.environ.get("LEZ_FUNDING", 10**10))
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def log(*a):
    print("[harness lez]", *a, flush=True)


def tool(name, *args, env=None):
    r = subprocess.run(
        [f"{TOOLS}/target/release/{name}", *args], cwd=TOOLS,
        capture_output=True, text=True,
        env={**os.environ, "HOME": HOME, "LEE_WALLET_HOME_DIR": HOME, **(env or {})},
    )
    if r.returncode != 0:
        raise RuntimeError(f"{name} exited {r.returncode}: {(r.stdout + r.stderr)[-600:]}")
    return r.stdout


def rpc(method, *params):
    req = urllib.request.Request(
        SEQUENCER,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)["result"]


def wait(what, cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return
        except (OSError, KeyError, ValueError):
            pass
        time.sleep(1)
    sys.exit(f"[harness lez] FAILED: {what} within {timeout}s")


def b58_hex(text):
    n = 0
    for c in text:
        n = n * 58 + B58.index(c)
    return n.to_bytes(32, "big").hex()


def init():
    os.makedirs(HOME, exist_ok=True)
    os.makedirs(f"{LEZ}/seq", exist_ok=True)
    payer = tool("mint_payer").split()[-1]
    with open(f"{LEZ}/payer", "w") as f:
        f.write(payer)
    # lez/sequencer/service/configs/docker/sequencer_config.json, with the payer
    # as the only genesis account and 500 ms blocks, so CLOCK_50 ticks in ~25 s.
    config = {
        "home": "/var/lib/sequencer_service",
        "max_num_tx_in_block": 20,
        "mempool_max_size": 10000,
        "block_create_timeout": "500ms",
        "retry_pending_blocks_timeout": "7s",
        "bedrock_config": {
            "channel_id": "01" * 32,
            "node_url": "http://host.docker.internal:18080",
            "funding_key": "2e03b2eff5a45478e7e79668d2a146cf2c5c7925bce927f2b1c67f2ab4fc0d26",
            "channel_params": {
                "minimum_sequencer_stake": 149,
                "posting_timeframe": 300,
                "posting_timeout": 25,
                "exit_delay": 100,
            },
        },
        "genesis": [{"supply_account": {"account_id": payer, "balance": GENESIS_BALANCE}}],
    }
    with open(f"{LEZ}/seq/sequencer_config.json", "w") as f:
        json.dump(config, f)
    with open(f"{HOME}/wallet_config.json", "w") as f:
        json.dump({
            "sequencer": {"sequencer_addr": SEQUENCER},
            "seq_poll_timeout": "500ms",
            "seq_tx_poll_max_blocks": 60,
            "seq_poll_max_retries": 10,
            "seq_block_poll_max_amount": 100,
        }, f)
    # The nodes, as root, and the sequencer, as uid 1000, both read and write here.
    subprocess.run(["chmod", "-R", "a+rwX", LEZ], check=False)
    log("payer", payer, "minted; sequencer config written")


def clock_ms():
    shard = rpc("getAccount", CLOCK_50)["data"]["shards"].get(CLOCK_SHARD, [])
    return int.from_bytes(bytes(shard[8:16]), "little")


def setup():
    with open(f"{LEZ}/payer") as f:
        payer = f.read().strip()
    wait("the sequencer produced no block", lambda: rpc("getLastBlockId") >= 1, 120)
    out = tool("run_setup", env={
        "LEZ_RLN_PAYER": payer,
        "LEZ_RLN_TREE_ID_HEX": secrets.token_hex(32),
        "RISC0_DEV_MODE": "1",
    })
    account = next((l.split()[-1] for l in out.splitlines() if l.startswith("Config account:")), None)
    if not account:
        sys.exit(f"[harness lez] FAILED: run_setup printed no config account:\n{out[-600:]}")
    registry = f"logos:local:{b58_hex(account)}"
    wait("CLOCK_50 never ticked", lambda: clock_ms() > 0, 180)
    os.makedirs(f"{LEZ}/fund", exist_ok=True)
    os.makedirs(f"{LEZ}/funded", exist_ok=True)
    subprocess.run(["chmod", "-R", "a+rwX", LEZ], check=False)
    # Published last: a node that reads it can ask for funding at once.
    with open(f"{LEZ}/registry-id.tmp", "w") as f:
        f.write(registry)
    os.rename(f"{LEZ}/registry-id.tmp", f"{LEZ}/registry-id")
    log("registry", registry, "deployed; CLOCK_50 live; funding on request")
    while True:
        for node in sorted(os.listdir(f"{LEZ}/fund")):
            done = f"{LEZ}/funded/{node}"
            if os.path.exists(done):
                continue
            with open(f"{LEZ}/fund/{node}") as f:
                account = f.read().strip()
            if not account:
                continue  # the node is still writing it
            try:
                tool("fund_account", "--to", account, "--amount", str(FUNDING),
                     env={"LEZ_RLN_PAYER": payer})
                result = "ok"
                log("funded", node, account)
            except RuntimeError as e:
                result = str(e)
                log("FAILED funding", node, account, result)
            with open(done, "w") as f:
                f.write(result)
        time.sleep(1)


if __name__ == "__main__":
    {"init": init, "setup": setup}[sys.argv[1]]()
