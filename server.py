import sys
import threading
import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib # pyright: ignore[reportAttributeAccessIssue]

BLUEZ_SERVICE_NAME = 'org.bluez'
GATT_MANAGER_IFACE = 'org.bluez.GattManager1'
LE_ADVERTISEMENT_IFACE = 'org.bluez.LEAdvertisement1'
LE_ADVERTISING_MANAGER_IFACE = 'org.bluez.LEAdvertisingManager1'
DBUS_OM_IFACE = 'org.freedesktop.DBus.ObjectManager'
DBUS_PROP_IFACE = 'org.freedesktop.DBus.Properties'
GATT_SERVICE_IFACE = 'org.bluez.GattService1'
GATT_CHRC_IFACE = 'org.bluez.GattCharacteristic1'
GATT_DESC_IFACE = 'org.bluez.GattDescriptor1'

class _InvalidArgsException(dbus.exceptions.DBusException):
    _dbus_error_name = 'org.freedesktop.DBus.Error.InvalidArgs'

class _Advertisement(dbus.service.Object):
    def __init__(self, bus, index):
        self.path = f'/org/bluez/example/advertisement{index}'
        self.bus = bus
        self.ad_type = 'peripheral'
        self.service_uuids = ['1812']
        self.local_name = 'Cobweb2194'
        self.appearance = 0x03C2 # Mouse/Keyboard combo
        dbus.service.Object.__init__(self, bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != LE_ADVERTISEMENT_IFACE:
            raise _InvalidArgsException()
        return {
            'Type': self.ad_type,
            'ServiceUUIDs': dbus.Array(self.service_uuids, signature='s'),
            'LocalName': dbus.String(self.local_name),
            'Appearance': dbus.UInt16(self.appearance),
            'IncludeTxPower': dbus.Boolean(False)
        }

    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature='', out_signature='')
    def Release(self):
        pass

class _Application(dbus.service.Object):
    def __init__(self, bus):
        self.path = '/'
        self.services = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_service(self, service):
        self.services.append(service)

    @dbus.service.method(DBUS_OM_IFACE, out_signature='a{oa{sa{sv}}}')
    def GetManagedObjects(self):
        response = {}
        for service in self.services:
            response[service.get_path()] = service.get_properties()
            for chrc in service.get_characteristics():
                response[chrc.get_path()] = chrc.get_properties()
                for desc in chrc.get_descriptors():
                    response[desc.get_path()] = desc.get_properties()
        return response

class _Service(dbus.service.Object):
    def __init__(self, bus, index, uuid, primary):
        self.path = f'/org/bluez/example/service{index}'
        self.bus = bus
        self.uuid = uuid
        self.primary = primary
        self.characteristics = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            GATT_SERVICE_IFACE: {
                'UUID': self.uuid,
                'Primary': self.primary,
                'Characteristics': dbus.Array([c.get_path() for c in self.characteristics], signature='o')
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_characteristic(self, characteristic):
        self.characteristics.append(characteristic)

    def get_characteristics(self):
        return self.characteristics

class _Characteristic(dbus.service.Object):
    def __init__(self, bus, index, uuid, flags, service):
        self.path = service.path + f'/char{index}'
        self.bus = bus
        self.uuid = uuid
        self.service = service
        self.flags = flags
        self.descriptors = []
        dbus.service.Object.__init__(self, bus, self.path)

    def add_descriptor(self, descriptor):
        self.descriptors.append(descriptor)

    def get_descriptors(self):
        return self.descriptors

    def get_properties(self):
        return {
            GATT_CHRC_IFACE: {
                'Service': self.service.get_path(),
                'UUID': self.uuid,
                'Flags': self.flags,
                'Descriptors': dbus.Array([d.get_path() for d in self.descriptors], signature='o')
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != GATT_CHRC_IFACE:
            raise _InvalidArgsException()
        return self.get_properties()[GATT_CHRC_IFACE]

    @dbus.service.signal(DBUS_PROP_IFACE, signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

class _Descriptor(dbus.service.Object):
    def __init__(self, bus, index, uuid, flags, characteristic):
        self.path = characteristic.path + f'/desc{index}'
        self.bus = bus
        self.uuid = uuid
        self.flags = flags
        self.chrc = characteristic
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            GATT_DESC_IFACE: {
                'Characteristic': self.chrc.get_path(),
                'UUID': self.uuid,
                'Flags': self.flags,
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != GATT_DESC_IFACE:
            raise _InvalidArgsException()
        return self.get_properties()[GATT_DESC_IFACE]

class _ReportReferenceDescriptor(_Descriptor):
    def __init__(self, bus, index, characteristic, report_id):
        _Descriptor.__init__(self, bus, index, '2908', ['read'], characteristic)
        self.value = dbus.Array([dbus.Byte(report_id), dbus.Byte(0x01)], signature='y')

    @dbus.service.method(GATT_DESC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        return self.value

class _ReportMapChrc(_Characteristic):
    def __init__(self, bus, index, service):
        _Characteristic.__init__(self, bus, index, '2A4B', ['read'], service)
        # Combined Map: Keyboard (ID 1) + Mouse (ID 2)
        # Modify this string to change the HID report descriptor. You can use https://eleccelerator.com/usbdescreqparser/ to help.
        hex_string = "05010906a1018501050719e029e71500250175019508810295017508810395067508150025650507190029658100c005010902a1010901a10085020509190129031500250195037501810295017505810305010930093109381581257f750895038106c0c0"
        self.value = dbus.Array([dbus.Byte(int(hex_string[i:i+2], 16)) for i in range(0, len(hex_string), 2)], signature='y')

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        return self.value

class _HidInfoChrc(_Characteristic):
    def __init__(self, bus, index, service):
        _Characteristic.__init__(self, bus, index, '2A4A', ['read'], service)
        self.value = dbus.Array([dbus.Byte(0x11), dbus.Byte(0x01), dbus.Byte(0x00), dbus.Byte(0x02)], signature='y')

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        return self.value

class _InputReportChrc(_Characteristic):
    def __init__(self, bus, index, service, report_id, default_payload):
        _Characteristic.__init__(self, bus, index, '2A4D', ['read', 'notify'], service)
        self.notifying = False
        self.report_id = report_id
        self.value = dbus.Array(default_payload, signature='y')
        self.add_descriptor(_ReportReferenceDescriptor(bus, 0, self, report_id))

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        return self.value

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        self.notifying = True

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        self.notifying = False

    def update_value(self, payload):
        if not self.notifying:
            return
        self.value = dbus.Array(payload, signature='y')
        self.PropertiesChanged(GATT_CHRC_IFACE, {'Value': self.value}, [])

class BLEHIDServer:
    def __init__(self):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self.adapter = self._find_adapter()
        if not self.adapter:
            raise RuntimeError("No BLE adapter found")

        self.app = _Application(self.bus)
        self.hid_service = _Service(self.bus, 0, '1812', True)
        
        self.report_map = _ReportMapChrc(self.bus, 0, self.hid_service)
        self.hid_info = _HidInfoChrc(self.bus, 1, self.hid_service)
        
        # Keyboard Report (ID 1, 8 bytes: [modifiers, reserved, key1..key6])
        self.kb_report = _InputReportChrc(self.bus, 2, self.hid_service, 1, [dbus.Byte(0)] * 8)
        
        # Mouse Report (ID 2, 4 bytes: [buttons, X, Y, wheel])
        self.mouse_report = _InputReportChrc(self.bus, 3, self.hid_service, 2, [dbus.Byte(0)] * 4)

        self.hid_service.add_characteristic(self.report_map)
        self.hid_service.add_characteristic(self.hid_info)
        self.hid_service.add_characteristic(self.kb_report)
        self.hid_service.add_characteristic(self.mouse_report)
        self.app.add_service(self.hid_service)

        self.ad = _Advertisement(self.bus, 0)
        self.obj = self.bus.get_object(BLUEZ_SERVICE_NAME, self.adapter)
        
        self.ad_manager = dbus.Interface(self.obj, LE_ADVERTISING_MANAGER_IFACE)
        self.gatt_manager = dbus.Interface(self.obj, GATT_MANAGER_IFACE)

    def _find_adapter(self):
        remote_om = dbus.Interface(self.bus.get_object(BLUEZ_SERVICE_NAME, '/'), DBUS_OM_IFACE)
        for o, props in remote_om.GetManagedObjects().items():
            if LE_ADVERTISING_MANAGER_IFACE in props and GATT_MANAGER_IFACE in props:
                return o
        return None

    def send_keyboard_report(self, modifiers, keys):
        """
        modifiers: 8-bit integer bitmask (e.g., 0x02 for Left Shift)
        keys: List of up to 6 USB HID keycodes (e.g., [0x04] for 'A')
        """
        payload = [dbus.Byte(modifiers), dbus.Byte(0)]
        for i in range(6):
            payload.append(dbus.Byte(keys[i] if i < len(keys) else 0))
        GLib.idle_add(self.kb_report.update_value, payload)

    def send_mouse_report(self, buttons, dx, dy, wheel=0):
        """
        buttons: 8-bit integer bitmask
        dx, dy, wheel: signed 8-bit integers (-127 to +127)
        """
        payload = [
            dbus.Byte(buttons),
            dbus.Byte(dx & 0xFF),
            dbus.Byte(dy & 0xFF),
            dbus.Byte(wheel & 0xFF),
        ]
        GLib.idle_add(self.mouse_report.update_value, payload)

    def start(self):
        self.ad_manager.RegisterAdvertisement(self.ad.get_path(), {}, reply_handler=lambda: None, error_handler=lambda e: sys.exit(1))
        self.gatt_manager.RegisterApplication(self.app.get_path(), {}, reply_handler=lambda: None, error_handler=lambda e: sys.exit(1))
        
        self.mainloop = GLib.MainLoop()
        try:
            self.mainloop.run()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        try:
            self.ad_manager.UnregisterAdvertisement(self.ad.get_path())
            self.gatt_manager.UnregisterApplication(self.app.get_path())
        except Exception:
            pass