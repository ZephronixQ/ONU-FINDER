import asyncio, telnetlib3, re, sys

# ================== SETTINGS ==================
SWITCHES = [
    "192.168.2.12",
    "192.168.2.13",
    "192.168.2.14",
    "192.168.2.16",
    "192.168.2.17",
    "192.168.2.18",
    "192.168.2.19",
]

USERNAME = "admin"
PASSWORD = "admin"
TELNET_PORT = 23
DOWN_RX_WARN_THRESHOLD = 27.0  # |Rx| > 27 → слабый сигнал

# ================== REGEX ==================
ANSI = re.compile(r'\x1B[@-_][0-?]*[ -/]*[@-~]')
GPON_SN_REGEX = re.compile(r'gpon-onu_(\d+/\d+/\d+:\d+)', re.I)
REMOTE_ID_REGEX = re.compile(r'port-location sub-option remote-id name (\S+) vport', re.I)
RX_RE = re.compile(r'Rx\s*:?\s*([-\d.]+)\(dbm\)', re.I)
TX_RE = re.compile(r'Tx\s*:?\s*([-\d.]+)\(dbm\)', re.I)
ATT_RE = re.compile(r'([\d.]+\(dB\))')

# ================== COLORS ==================
GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
MAGENTA = "\033[95m"
BLUE = "\033[94m"
RESET = "\033[0m"

# ================== UTILS ==================
def clean_line(line: str) -> str:
    line = ANSI.sub('', line)
    line = re.sub(r"[^\x20-\x7E]+", " ", line)
    line = re.sub(r"\s+", " ", line)
    return line.strip()

def strip_ansi(s: str) -> str:
    return ANSI.sub('', s)

# ================== TELNET ==================
async def telnet_connect(host: str):
    reader, writer = await telnetlib3.open_connection(
        host=host,
        port=TELNET_PORT,
        connect_minwait=0.5,
        connect_maxwait=3
    )
    writer.write(USERNAME + "\n")
    await asyncio.sleep(0.2)
    writer.write(PASSWORD + "\n")
    await asyncio.sleep(0.5)
    return reader, writer

# ================== BULK COMMANDS ==================
async def send_bulk_commands(reader, writer, commands, timeout=2):
    marker = "===END==="
    payload = "\n".join(commands + [f"echo {marker}"]) + "\n"
    writer.write(payload)
    output = ""
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout)
            if not chunk:
                break
            output += chunk
            if marker in chunk:
                break
    except asyncio.TimeoutError:
        pass
    return output

# ================== PARSER ==================
def parse_onu_interface(output: str):
    for line in output.splitlines():
        m = GPON_SN_REGEX.search(clean_line(line))
        if m:
            return m.group(1)
    return None

def parse_remote_id(output: str):
    for line in output.splitlines():
        m = REMOTE_ID_REGEX.search(clean_line(line))
        if m:
            return m.group(1)
    return None

def parse_pon_power(output: str):
    rows = []
    for raw in output.splitlines():
        line = clean_line(raw)
        if not line.lower().startswith(("up", "down")):
            continue
        direction = "UP" if line.lower().startswith("up") else "DOWN"
        rx = RX_RE.search(line)
        tx = TX_RE.search(line)
        att = ATT_RE.search(line)
        if not (rx and tx and att):
            continue
        rx_val = float(rx.group(1))
        tx_val = float(tx.group(1))
        if direction == "UP":
            olt = f"Rx {rx_val}(dbm)"
            onu = f"Tx {tx_val}(dbm)"
            onu_rx_value = None
        else:
            olt = f"Tx {tx_val}(dbm)"
            onu = f"Rx {rx_val}(dbm)"
            onu_rx_value = abs(rx_val)
        rows.append({
            "direction": direction,
            "olt": olt,
            "onu": onu,
            "attenuation": att.group(1),
            "onu_rx": onu_rx_value
        })
    return rows

def parse_ip_service(output: str):
    for line in output.splitlines():
        line = clean_line(line)
        if re.match(r'^\d+\s+\d+\.\d+\.\d+\.\d+', line):
            parts = line.split()
            if len(parts) >= 5:
                return {"ip": parts[1], "mac": parts[2], "vlan": parts[3]}
    return {"ip": "-", "mac": "-", "vlan": "-"}

def parse_remote_onu_interface(output: str):
    operate = speed = "-"
    for line in output.splitlines():
        line = clean_line(line)
        if line.lower().startswith("operate status"):
            operate = line.split()[-1]
        elif line.lower().startswith("speed status"):
            speed = line.split()[-1]
    return {"operate": operate, "speed": speed}

def parse_interface_speed(output: str):
    input_rate_bps = output_rate_bps = 0.0
    for line in output.splitlines():
        line = clean_line(line)
        if line.lower().startswith("input rate"):
            m = re.search(r'Input rate\s*:\s*([\d.]+)\s*Bps', line, re.I)
            if m:
                input_rate_bps = float(m.group(1)) * 8 / 1_000_000
        elif line.lower().startswith("output rate"):
            m = re.search(r'Output rate\s*:\s*([\d.]+)\s*Bps', line, re.I)
            if m:
                output_rate_bps = float(m.group(1)) * 8 / 1_000_000
    return {"input_mbps": round(input_rate_bps, 3), "output_mbps": round(output_rate_bps, 3)}

def parse_onu_detail_logs(output: str):
    logs = []
    pattern = re.compile(
        r'^\s*(\d+)\s+([\d-]+\s+[\d:]+)\s+([\d-]+\s+[\d:]+)?\s*(\S+)?',
        re.MULTILINE
    )
    for m in pattern.finditer(output):
        num = m.group(1)
        auth_time = m.group(2)
        offline_time = m.group(3) if m.group(3) else "-"
        cause = m.group(4) if m.group(4) else "-"
        logs.append({
            "num": num,
            "auth_time": auth_time,
            "offline_time": offline_time,
            "cause": cause
        })
    return logs

# ================== COLLECT DATA ==================
async def collect_onu_data(reader, writer, iface):
    commands = [
        "terminal length 0",
        f"show running-config interface gpon-onu_{iface}",
        f"show pon power attenuation gpon-onu_{iface}",
        f"show ip-service user status gpon-onu_{iface}",
        f"show gpon remote-onu interface eth gpon-onu_{iface}",
        f"show interface gpon-onu_{iface}",
        f"show gpon onu detail-info gpon-onu_{iface}",
    ]
    raw = await send_bulk_commands(reader, writer, commands)
    return {
        "remote_id": parse_remote_id(raw),
        "pon_power": parse_pon_power(raw),
        "ip_service": parse_ip_service(raw),
        "remote_onu": parse_remote_onu_interface(raw),
        "iface_speed": parse_interface_speed(raw),
        "detail_logs": parse_onu_detail_logs(raw),
    }

# ================== SEARCH ==================
async def search_onu_on_switch(host: str, serial: str):
    try:
        reader, writer = await telnet_connect(host)
        out = await send_bulk_commands(reader, writer, [f"show gpon onu by sn {serial}"])
        iface = parse_onu_interface(out)
        if not iface:
            writer.close()
            return None
        data = await collect_onu_data(reader, writer, iface)
        writer.close()
        return {
            "host": host,
            "serial": serial,
            "interface": iface,
            **data
        }
    except Exception:
        return None

async def find_onu(serial: str):
    tasks = [asyncio.create_task(search_onu_on_switch(sw, serial)) for sw in SWITCHES]
    for task in asyncio.as_completed(tasks):
        result = await task
        if result:
            for t in tasks:
                t.cancel()
            return result
    return None

# ================== TABLES ==================
def print_onu_table(d):
    headers = ["SWITCH IP", "SERIAL", "PORT", "ID"]
    row = [
        f"{GREEN}{d['host']}{RESET}",
        f"{MAGENTA}{d['serial']}{RESET}",
        d["interface"],
        f"{YELLOW}{d['remote_id']}{RESET}" if d["remote_id"] else "-"
    ]
    w = [max(len(headers[i]), len(strip_ansi(row[i]))) + 4 for i in range(4)]
    print("┌" + "┬".join("─"*x for x in w) + "┐")
    print("│" + "│".join(f"{BLUE}{headers[i].center(w[i])}{RESET}" for i in range(4)) + "│")
    print("├" + "┼".join("─"*x for x in w) + "┤")
    print("│" + "│".join(row[i].center(w[i] + len(row[i]) - len(strip_ansi(row[i]))) for i in range(4)) + "│")
    print("└" + "┴".join("─"*x for x in w) + "┘")

def print_pon_power_table(rows):
    headers = ["", "OLT", "ONU", "ATTENUATION"]
    w = [12, 30, 22, 22]
    print("┌" + "┬".join("─"*x for x in w) + "┐")
    print("│" + "│".join(f"{BLUE}{headers[i].center(w[i])}{RESET}" for i in range(4)) + "│")
    print("├" + "┼".join("─"*x for x in w) + "┤")
    for i, r in enumerate(rows):
        row = [
            f"{GREEN if r['direction']=='UP' else RED}{r['direction']}{RESET}",
            f"{CYAN}{r['olt']}{RESET}",
            f"{MAGENTA}{r['onu']}{RESET}",
            f"{YELLOW}{r['attenuation']}{RESET}",
        ]
        print("│" + "│".join(row[j].center(w[j] + len(row[j]) - len(strip_ansi(row[j]))) for j in range(4)) + "│")
        if i < len(rows)-1:
            print("├" + "┼".join("─"*x for x in w) + "┤")
    print("└" + "┴".join("─"*x for x in w) + "┘")

def print_ip_table(ip_service):
    headers = ["IP", "MAC", "VLAN"]
    w = [16, 20, 8]
    row = [
        f"{CYAN}{ip_service['ip']}{RESET}",
        f"{MAGENTA}{ip_service['mac']}{RESET}",
        f"{YELLOW}{ip_service['vlan']}{RESET}"
    ]
    print("┌" + "┬".join("─"*x for x in w) + "┐")
    print("│" + "│".join(f"{BLUE}{headers[i].center(w[i])}{RESET}" for i in range(3)) + "│")
    print("├" + "┼".join("─"*x for x in w) + "┤")
    print("│" + "│".join(row[i].center(w[i] + len(row[i]) - len(strip_ansi(row[i]))) for i in range(3)) + "│")
    print("└" + "┴".join("─"*x for x in w) + "┘")

def print_oper_speed_table(remote_onu, iface_speed):
    headers = ["Operate status", "Speed status", "Input rate (Mbit/s)", "Output rate (Mbit/s)"]
    w = [18, 15, 20, 20]
    row = [
        f"{GREEN if remote_onu['operate'].lower()=='enable' else RED}{remote_onu['operate']}{RESET}",
        f"{GREEN if remote_onu['speed'].lower()!='disable' else RED}{remote_onu['speed']}{RESET}",
        f"{CYAN}{iface_speed['input_mbps']}{RESET}",
        f"{MAGENTA}{iface_speed['output_mbps']}{RESET}"
    ]
    print("┌" + "┬".join("─"*x for x in w) + "┐")
    print("│" + "│".join(f"{BLUE}{headers[i].center(w[i])}{RESET}" for i in range(4)) + "│")
    print("├" + "┼".join("─"*x for x in w) + "┤")
    print("│" + "│".join(row[i].center(w[i] + len(row[i]) - len(strip_ansi(row[i]))) for i in range(4)) + "│")
    print("└" + "┴".join("─"*x for x in w) + "┘")

def print_onu_detail_logs_table(logs):
    headers = ["#", "Authpass Time", "Offline Time", "Cause"]
    w = [4, 22, 22, 12]
    print("┌" + "┬".join("─"*x for x in w) + "┐")
    print("│" + "│".join(f"{BLUE}{headers[i].center(w[i])}{RESET}" for i in range(4)) + "│")
    print("├" + "┼".join("─"*x for x in w) + "┤")
    for i, log in enumerate(logs):
        cause_val = log['cause'] if log['cause'] not in ("", None) else " "
        offline_val = log['offline_time'] if log['offline_time'] not in ("", None) else " "
        row = [
            f"{MAGENTA}{log['num']}{RESET}",
            f"{CYAN}{log['auth_time']}{RESET}",
            f"{CYAN}{offline_val}{RESET}",
            f"{YELLOW}{cause_val}{RESET}"
        ]
        print("│" + "│".join(row[j].center(w[j] + len(row[j]) - len(strip_ansi(row[j]))) for j in range(4)) + "│")
        if i < len(logs)-1:
            print("├" + "┼".join("─"*x for x in w) + "┤")
    print("└" + "┴".join("─"*x for x in w) + "┘")

# ================== MAIN ==================
async def main():
    if len(sys.argv) != 2:
        print("Usage: python3 onu_sn_finder.py <ONU_SERIAL>")
        return
    result = await find_onu(sys.argv[1])
    if not result:
        print(f"{RED}❌ ONU not found{RESET}")
        return
    print(f"\n{GREEN}✅ ONU FOUND{RESET}")
    print_onu_table(result)

    # ===== IP STATUS =====
    if result["ip_service"]:
        print(f"\n{CYAN}🌐 IP STATUS{RESET}")
        print_ip_table(result["ip_service"])
        ip_info = result["ip_service"]
        if ip_info["ip"] == "-" or ip_info["mac"] == "-":
            print(f"{YELLOW}No IP/MAC data found for this ONU{RESET}")
        elif "," in ip_info["mac"] or len(ip_info["mac"].split()) > 1:
            print(f"{YELLOW}Multiple MAC addresses detected — possible LAN/WAN mix-up{RESET}")
    else:
        print(f"{YELLOW}No IP status information available{RESET}")

    # ===== PON POWER =====
    if result["pon_power"]:
        print(f"\n{CYAN}📡 PON POWER LEVELS{RESET}")
        print_pon_power_table(result["pon_power"])
        for r in result["pon_power"]:
            if r["direction"] == "DOWN" and r["onu_rx"] is not None:
                if r["onu_rx"] > DOWN_RX_WARN_THRESHOLD:
                    print(f"\n{RED}WARNING:{RESET} LOW DOWNSTREAM SIGNAL — -{r['onu_rx']}(dBm)")

    # ===== OPERATE / SPEED / THROUGHPUT =====
    if result["remote_onu"] and result["iface_speed"]:
        print(f"\n{CYAN}⚡ OPERATE / SPEED / THROUGHPUT{RESET}")
        print_oper_speed_table(result["remote_onu"], result["iface_speed"])
        operate = result["remote_onu"]["operate"].lower()
        speed = result["remote_onu"]["speed"].lower()
        ip_mac = result["ip_service"]["mac"] if result["ip_service"] else None

        if operate == "enable" and ip_mac and ip_mac != "-":
            print(f"{YELLOW}ONU is enabled but MAC detected — possible router reset{RESET}")
        elif operate == "disable":
            print(f"{YELLOW}ONU operate status is disabled — device not turned on{RESET}")

        if speed not in ("full-100", "100full", "full-1000", "1000full"):
            print(f"{YELLOW}ONU speed is not [Full-100/1000] — current: {speed}{RESET}")

    # ===== DETAIL LOGS =====
    if result.get("detail_logs"):
        print(f"\n{CYAN}📝 ONU DETAIL LOGS{RESET}")
        print_onu_detail_logs_table(result["detail_logs"])

        los_alerts = []
        for i, log in enumerate(result["detail_logs"]):
            cause = (log.get("cause") or "").strip().lower()
            is_last_nonempty = True
            for j in range(i + 1, len(result["detail_logs"])):
                if (result["detail_logs"][j].get("cause") or "").strip():
                    is_last_nonempty = False
                    break

            if cause in ("los", "losi") and is_last_nonempty:
                los_alerts.append(log)

        if los_alerts:
            print(f"\n{RED}LOS/LOSi detected in last cause entry — possible fiber issue, send technician{RESET}")

if __name__ == "__main__":
    asyncio.run(main())
