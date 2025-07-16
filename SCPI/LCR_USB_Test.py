import pyvisa

rm = pyvisa.ResourceManager()  
# this will list something like 'USB0::0x1234::0x5678::INSTR'
print("VISA resources:", rm.list_resources())

# pick the USB resource string that starts with 'USB0'
inst = rm.open_resource(rm.list_resources()[0])
inst.timeout            = 5000    # ms
inst.write_termination  = "\n"
inst.read_termination   = "\n"
print("IDN:", inst.query("*IDN?"))
