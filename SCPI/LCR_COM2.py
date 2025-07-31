import pyvisa
import time

def main():
    rm = pyvisa.ResourceManager("@py")
    devs = rm.list_resources()
    print("VISA Devices:", devs)
    target = next((d for d in devs if d.startswith("USB")), None)
    if not target:
        print("No USB instrument found.")
        return

    instr = rm.open_resource(target, timeout=8000)
    instr.write_termination = '\n'
    instr.read_termination = '\n'

    print("ID:", instr.query("*IDN?").strip())

    instr.write("*CLS")
    time.sleep(0.2)
    print("After clear:", instr.query("SYST:ERR?").strip())
    print("Status bits:", instr.query("STAT:OPER:COND?").strip())

    instr.write("FUNC:IMP CPD")
    instr.write("TRIG:SOUR IMM")
    instr.write("AVER:STAT OFF")
    time.sleep(0.2)

    print("Triggering measurement")
    instr.write("INIT")
    instr.write("*WAI")

    print("Post-measure status:", instr.query("SYST:ERR?").strip())
    print("Status bits:", instr.query("STAT:OPER:COND?").strip())

    try:
        cap = instr.query("FETC:IMP:CAP?").strip()
        print("Capacitance:", cap, "F")
    except Exception as e:
        print("Fetch error:", e)

    instr.close()

if __name__ == "__main__":
    main()
