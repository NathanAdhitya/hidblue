# hidblue - a bluetooth HID device emulator for Linux + Wayland

## Why?

Two machines, one keyboard, one mouse, and no KVM switch. The twist? The second machine is locked down in some way or another. But bluetooth works! Now if only there was a way to make the first machine's keyboard and mouse appear as a bluetooth HID device to the second machine... 👀

## Features?

- Uses BLE (HID over GATT) to emulate a bluetooth HID device, so it should work with any modern machine that supports bluetooth.
- Uses evdev to read input events.
- Included is an unreliable mouse jiggler to (hopefully) prevent the second machine from going to sleep.
- Keybind to switch between the two machines on the fly (grabs input, hardcoded to F3).
- Remote paste (text currently in clipboard will be typed out on the second machine, hardcoded to F3+V)
- \*F3 is not passed to the second machine. If this is a dealbreaker for you, edit it in the `handle_keyboard_event` function in `bridge.py`.
- It's just 3 Python files.
- Tested running python3 as root. In theory, it should be possible to run as a regular user with the right permissions, but I haven't tested that yet.

## Notes

Tested with Ubuntu 24.04 on GNOME Wayland, connected to a Windows 11 machine.
Mouse isn't the smoothest, but it works for me.
Connection may be finnicky, I personally have to re-pair the devices every time I want to use it, but YMMV. If you have any tips on how to make the connection more stable, please let me know!

## Quick start

### Guide for Ubuntu 24.04

1. Disable `input` from being handled by default. Run `systemctl edit bluetooth`, and add the following lines:

    ```ini
    [Service]
    ExecStart=
    ExecStart=/usr/libexec/bluetooth/bluetoothd -P input
    ```

    Then run `systemctl restart bluetooth` to apply the changes.

2. I used system wide packages, so

    ```bash
    sudo apt update && sudo apt install python3-evdev python3-gi python3-dbus -y
    ```

3. Run `sudo python3 bridge.py` to start the server. In my scenario, I had to have GNOME Bluetooth settings open to get the pairing code and connect both ends. Once connected, you can try pressing F3 to switch, then try moving the mouse or typing on the second machine. If it doesn't work, try re-pairing the devices.

## Alternatives

- Use [deskflow](https://github.com/deskflow/deskflow) or it's [siblings](https://github.com/deskflow/deskflow/wiki/Project-Forks) if you can install software on both machines and both are connected within the same Ethernet network.
- Use a proper hardware KVM switch? They're pretty cheap nowadays, but it's extra cables and space taken up on your desk.
- Is your (machine|brain|software) too old for this? Try the original implementations where this all started: [hidclient](https://github.com/4ndrej/hidclient) and or [EmuBTHID](https://github.com/Alkaid-Benetnash/EmuBTHID).

## Credits

Source code ~~stolen from~~ inspired by [hidclient](https://github.com/4ndrej/hidclient) and [EmuBTHID](https://github.com/Alkaid-Benetnash/EmuBTHID). Adapted with the help of multiple LLM models.
