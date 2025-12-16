import asyncio, telnetlib3, re, sys

# ================== НАСТРОЙКИ ==================
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

# ================== REGEX ==================
ANSI = re.compile(r'\x1B[@-_][0-?]*[ -/]*[@-~]')
GPON_SN_REGEX = re.compile(r'gpon-onu_(\d+/\d+/\d+:\d+)', re.I)
REMOTE_ID_REGEX = re.compile(r'port-location sub-option remote-id name (\S+) vport', re.I)

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
    """Очищает строку от ANSI кодов и лишних пробелов"""
    line = ANSI.sub('', line)
    line = re.sub(r"[^\x20-\x7E]+", " ", line)
    line = re.sub(r"\s+", " ", line)
    return line.strip()

def strip_ansi(s: str) -> str:
    """Убирает ANSI-коды для корректного подсчета длины"""
    return re.sub(r'\x1B[@-_][0-?]*[ -/]*[@-~]', '', s)

# ================== TELNET ==================
async def telnet_connect(host: str, password: str):
    reader, writer = await telnetlib3.open_connection(
        host=host,
        port=TELNET_PORT,
        connect_minwait=0.5,
        connect_maxwait=3
    )
    writer.write(USERNAME + "\n")
    await asyncio.sleep(0.5)
    writer.write(password + "\n")
    await asyncio.sleep(0.8)
    return reader, writer

async def send_command(reader, writer, command: str, timeout=2.5) -> str:
    """Отправка команды и сбор вывода"""
    writer.write(command + "\n")
    await asyncio.sleep(0.4)

    output = ""
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            if not chunk:
                break
            output += chunk
            if "---- More ----" in chunk or "more" in chunk.lower():
                writer.write(" ")
                await asyncio.sleep(0.3)
        except asyncio.TimeoutError:
            break
    return output

# ================== PARSER ==================
def parse_onu_interface(output: str) -> str | None:
    """Парсит вывод команды и возвращает только порт GPON ONU в формате X/X/X:X"""
    for raw_line in output.splitlines():
        line = clean_line(raw_line)
        if not line:
            continue
        match = GPON_SN_REGEX.search(line)
        if match:
            return match.group(1)
    return None

def parse_remote_id(output: str) -> str | None:
    """Парсит вывод running-config и возвращает значение после 'name' и до 'vport'"""
    for raw_line in output.splitlines():
        line = clean_line(raw_line)
        match = REMOTE_ID_REGEX.search(line)
        if match:
            return match.group(1)
    return None

# ================== SEARCH ==================
async def search_onu_on_switch(host: str, serial: str):
    try:
        reader, writer = await telnet_connect(host, PASSWORD)
        output = await send_command(reader, writer, f"show gpon onu by sn {serial}")
        iface = parse_onu_interface(output)
        if iface:
            output_cfg = await send_command(reader, writer, f"show running-config interface gpon-onu_{iface}")
            remote_id = parse_remote_id(output_cfg)
            writer.close()
            return {
                "host": host,
                "serial": serial,
                "interface": iface,
                "remote_id": remote_id
            }
        writer.close()
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

# ================== TABLE PRINT ==================
def print_table_dynamic(data):
    """Выводит таблицу с динамическими колонками, заголовки и значения по центру с цветами"""
    headers = ["SWITCH IP", "SERIAL", "PORT", "ID"]
    row = [
        f"{GREEN}{data['host']}{RESET}",
        f"{MAGENTA}{data['serial']}{RESET}",
        f"{data['interface']}",  # стандартный белый
        f"{YELLOW}{data['remote_id']}{RESET}" if data['remote_id'] else "-"
    ]

    # вычисляем ширину колонок: максимум длины заголовка и данных + небольшой запас
    col_widths = [
        max(len(headers[0]), len(strip_ansi(row[0]))) + 4,
        max(len(headers[1]), len(strip_ansi(row[1]))) + 4,
        max(len(headers[2]), len(strip_ansi(row[2]))) + 4,
        max(len(headers[3]), len(strip_ansi(row[3]))) + 4,
    ]

    # печать шапки синим
    print("┌" + "┬".join("─"*w for w in col_widths) + "┐")
    print("│" + "│".join(f"{BLUE}{headers[i].center(col_widths[i])}{RESET}" for i in range(4)) + "│")
    print("├" + "┼".join("─"*w for w in col_widths) + "┤")

    # печать строки данных с учетом ANSI
    print("│" + "│".join(row[i].center(col_widths[i] + len(row[i]) - len(strip_ansi(row[i]))) for i in range(4)) + "│")
    print("└" + "┴".join("─"*w for w in col_widths) + "┘")

# ================== MAIN ==================
async def main():
    if len(sys.argv) != 2:
        print(f"{YELLOW}Usage: python3 onu_sn_finder.py <ONU_SERIAL>{RESET}")
        sys.exit(1)

    serial = sys.argv[1]
    print(f"\n🔍 Searching ONU serial: {serial}\n")

    result = await find_onu(serial)

    if result:
        print(f"{GREEN}✅ ONU FOUND{RESET}\n")
        print_table_dynamic(result)
        print()
    else:
        print(f"{RED}❌ ONU not found on any switch{RESET}\n")

if __name__ == "__main__":
    asyncio.run(main())
