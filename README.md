# Multibridge Pro 2

A web page for changing settings on a Blackmagic Multibridge Pro 2 from a modern Mac, including Apple Silicon. The official Multibridge Utility cannot see the box on these Macs because its driver will not load. This program talks to the converter over USB itself.

It is for the Multibridge Pro 2 only. Leave the PCI Express cable unplugged. The box is a standalone converter in that mode, and the missing PCI Express card is not required.

## Install

1. Install [Blackmagic Multibridge Utility](https://www.blackmagicdesign.com/) the way you already have it. This download does not include Blackmagic’s USB startup image or converter image. Those files stay in the official install, and this program reads them from there when it needs them.
2. Install Python 3 from [python.org](https://www.python.org/downloads/) or with Homebrew: `brew install python`
3. Install libusb. If you do not have Homebrew, install it from [brew.sh](https://brew.sh), then run:

```bash
brew install libusb
```

4. Download this project and unzip it.

## Run

Power the Multibridge Pro 2, connect it with a USB 2.0 cable, and leave the PCI Express cable unplugged. Quit the official Multibridge Utility if it is open.

Double-click `Start Multibridge.command`. If macOS says it cannot be opened because it was downloaded, right-click the file, choose Open, then Open again.

Or, in Terminal:

```bash
cd /path/to/multibridge
python3 mb.py serve
```

Then open http://127.0.0.1:8765

The page stays on this Mac. Nothing is sent to the internet.

Changes apply as soon as you pick them. Use **Save power-up settings** when they should still be there after the converter is turned off.

## What it will not change

The USB identity of the box is left alone, so it still enumerates as a Multibridge Pro 2. Saving settings writes the converter’s power-up memory. It does not replace the converter image when you only change a menu.

`python3 mb.py load-fpga` sends the converter image from the installed utility. Use that only when the page says the converter has not come up. It takes about half a minute.

## Other commands

```bash
python3 mb.py status
python3 mb.py load-fpga
```
