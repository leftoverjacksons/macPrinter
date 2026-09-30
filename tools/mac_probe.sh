#!/usr/bin/env bash
# Print identity details for every USB network interface.
# Run after each plug-in (sudo recommended: EEPROM dump and dmesg need root).
#
#   sudo ./tools/mac_probe.sh
#
# Interpreting the output:
#   addr_assign_type 0 = permanent (from the dongle)  -> good
#   addr_assign_type 1 = random (driver invented it)  -> dongle has no valid MAC
#   addr_assign_type 3 = set by host software         -> host is overriding it
#   "locally administered: yes" on a random address is expected; on a
#   supposedly permanent address it means the vendor did not use a real OUI.
#   EEPROM bytes all ff / all 00 -> blank or absent EEPROM.
set -u

found=0
for dev in /sys/class/net/*; do
    iface=$(basename "$dev")
    [ -e "$dev/device" ] || continue
    intf=$(readlink -f "$dev/device")
    case "$intf" in *usb*) ;; *) continue ;; esac
    usb=$(dirname "$intf")      # USB interface dir -> USB device dir
    found=1

    mac=$(cat "$dev/address")
    first=$((16#${mac%%:*}))
    case $(cat "$dev/addr_assign_type") in
        0) aat="0 (permanent)" ;;
        1) aat="1 (RANDOM)" ;;
        2) aat="2 (stolen)" ;;
        3) aat="3 (set by host)" ;;
        *) aat="unknown" ;;
    esac

    echo "=== $iface ==="
    echo "  mac                 : $mac"
    echo "  addr_assign_type    : $aat"
    echo "  locally administered: $([ $((first & 2)) -ne 0 ] && echo yes || echo no)"
    if command -v ethtool >/dev/null; then
        echo "  ethtool permanent   : $(ethtool -P "$iface" 2>/dev/null | awk '{print $3}')"
    fi
    echo "  driver              : $(basename "$(readlink -f "$dev/device/driver")")"
    echo "  usb vid:pid         : $(cat "$usb/idVendor" 2>/dev/null):$(cat "$usb/idProduct" 2>/dev/null)"
    echo "  usb bcdDevice       : $(cat "$usb/bcdDevice" 2>/dev/null)"
    echo "  usb manufacturer    : $(cat "$usb/manufacturer" 2>/dev/null)"
    echo "  usb product         : $(cat "$usb/product" 2>/dev/null)"
    echo "  usb serial          : $(cat "$usb/serial" 2>/dev/null || echo '(none)')"
    if [ "$(id -u)" -eq 0 ] && command -v ethtool >/dev/null; then
        echo "  eeprom (first 32 B) :"
        ethtool -e "$iface" length 32 2>&1 | sed 's/^/    /'
    fi
done

[ "$found" -eq 1 ] || echo "No USB network interfaces found."

if [ "$(id -u)" -eq 0 ]; then
    echo "=== recent kernel messages ==="
    dmesg | grep -iE 'ax88179|ax_usb|cdc_ncm|invalid MAC' | tail -n 10
fi
