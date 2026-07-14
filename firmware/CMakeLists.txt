# RP2350-LCD-1.47 USB-serial image display firmware.
#
# Board: Waveshare RP2350-LCD-1.47-A (RP2350A). The upstream Raspberry Pi
# "pico2" board definition matches the RP2350A well enough for our purposes
# (the LCD/backlight pins are configured explicitly in lib/Config/DEV_Config.*).
set(PICO_BOARD pico2 CACHE STRING "Board type")

cmake_minimum_required(VERSION 3.13)

# Locate the Pico SDK. PICO_SDK_PATH is provided by the Nix build (see
# flake.nix) or by the developer's environment.
include(pico_sdk_import.cmake)

project(rp2350_lcd_usb_image C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

pico_sdk_init()

add_executable(firmware
    main.c
    lib/Config/DEV_Config.c
    lib/LCD/LCD_1in47.c
)

target_include_directories(firmware PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}/lib/Config
    ${CMAKE_CURRENT_LIST_DIR}/lib/LCD
)

# Serial link is the USB CDC ACM port; no UART stdio.
pico_enable_stdio_usb(firmware 1)
pico_enable_stdio_uart(firmware 0)

# Produce firmware.uf2 (plus .elf/.bin/.map/.hex) for drag-and-drop flashing.
pico_add_extra_outputs(firmware)

target_link_libraries(firmware
    pico_stdlib
    hardware_spi
    hardware_pwm
    hardware_i2c
)
