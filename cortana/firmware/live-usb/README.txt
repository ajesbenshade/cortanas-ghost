HARMAN KARDON INVOKE - FLASHING ENCORE FROM AN UBUNTU LIVE USB
================================================================

What this folder is
-------------------
  flash.sh      starts Harman's loader and opens the speaker's console
  console.py    the console (replaces PuTTY)
  uboot/        Harman's loader and boot files + the Encore firmware (83_IMAGE)
  stock/        Harman's original firmware, for going back
  SHA256SUMS    checksums; flash.sh verifies every file before starting

Nothing here touches the speaker's bootloader or kernel. The only command
that writes to the speaker is the tftp2nand line you type yourself.


PART 1 - Make the stick (in Windows)
------------------------------------
  1. Download the Ubuntu Desktop 24.04 LTS ISO from ubuntu.com.
  2. Write it to a USB stick (8 GB or larger) with Rufus, default settings.
     This erases the stick.
  3. Copy this whole "invoke-flash" folder onto the stick afterwards, or
     onto a second stick.


PART 2 - Boot Ubuntu
--------------------
  1. Leave the speaker's USB cable plugged into a rear USB 2.0 (black) port.
  2. Restart the PC and press F11 repeatedly for the boot menu
     (Del opens the BIOS if F11 does not). Pick the USB stick.
  3. Choose "Try Ubuntu". Do NOT choose Install.
  4. Open the Files app, find the invoke-flash folder, right-click inside
     it and choose "Open in Terminal".


PART 3 - Flash
--------------
  1. In the terminal run:

         bash flash.sh

  2. When it says the loader is waiting, put the speaker in service mode:
       - unplug the speaker's power (leave USB connected)
       - hold the reset pinhole and plug power back in
       - still holding reset, press mic-mute 4 times
       - release when the ring is yellow

  3. Text scrolls and stops at a prompt ending in  >>
     Type these by hand, one at a time, Enter after each:

         usbload 0x83 0x7000000
         tftp2nand -d 69481562 0x7000000
         reset

     - Wait for >> to come back after usbload before going on.
     - tftp2nand prints "writing to NAND" and takes a few minutes.
       Do not unplug anything while it runs.
     - NEVER add -m to tftp2nand. That variant erases the whole chip,
       bootloader included.
     - If you make a typo, the bootloader just reports an error. Retype it.

  4. After "reset" the speaker boots into Encore. Press Ctrl+] to close
     the console.

  Before tftp2nand, you can back out at any time by unplugging the
  speaker's power. Nothing has been written until that command runs.


PART 4 - First boot of Encore
-----------------------------
  1. On a phone or laptop, join the Wi-Fi network named  Invoke-XXXX
     (password: ridiculous).
  2. A setup page opens. Give it your home Wi-Fi details.
  3. Shut down Ubuntu, remove the stick, boot back into Windows.
  4. Change the speaker's root password from the dashboard: the default
     is the same on every Encore speaker.


If something goes wrong
-----------------------
  Nothing scrolls after the ring turns yellow
      Run  lsusb  in a second terminal. You should see "1286:8174".
      If not: try another rear USB 2.0 port or another data cable, then
      redo the reset sequence. Leave flash.sh running while you do.

  The console shows text but never reaches >>
      Press Ctrl+], unplug the speaker's power, and start again from
      "bash flash.sh".

  The speaker does not boot after flashing
      Service mode still works, because the bootloader was not touched.
      Flash again, or return to Harman's firmware with:

          bash flash.sh stock

      and type the same three commands.

  Logs are kept in  ~/invoke-flash-work/  (console.log, loader.log).
  They disappear when the live session ends, so copy them to the stick
  if you want help reading them afterwards.
