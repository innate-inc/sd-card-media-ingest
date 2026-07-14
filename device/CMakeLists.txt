# RP2350-LCD-1.47 device firmware running the shared LVGL UI.
set(PICO_BOARD pico2 CACHE STRING "Board type")

cmake_minimum_required(VERSION 3.13)
include(pico_sdk_import.cmake)
project(ingest_device C CXX ASM)
set(CMAKE_C_STANDARD 11)
pico_sdk_init()

# LVGL source tree (provided by Nix or -DLVGL_DIR=...).
if(NOT DEFINED LVGL_DIR)
  set(LVGL_DIR $ENV{LVGL_DIR})
endif()
if(NOT LVGL_DIR)
  message(FATAL_ERROR "set -DLVGL_DIR=/path/to/lvgl")
endif()

include_directories(
  ${CMAKE_CURRENT_LIST_DIR}/../app
  ${CMAKE_CURRENT_LIST_DIR}/../firmware/lib/Config
  ${CMAKE_CURRENT_LIST_DIR}/../firmware/lib/LCD
)
add_compile_definitions(LV_CONF_INCLUDE_SIMPLE)

# EXCLUDE_FROM_ALL: skip LVGL's own install rules. Strip NEON/Helium .S (those
# are for Cortex-A, not the RP2350's Cortex-M33; we use LV_DRAW_SW_ASM_NONE).
add_subdirectory(${LVGL_DIR} lvgl_build EXCLUDE_FROM_ALL)
get_target_property(_lvgl_srcs lvgl SOURCES)
if(_lvgl_srcs)
  list(FILTER _lvgl_srcs EXCLUDE REGEX "(neon|helium)")
  set_target_properties(lvgl PROPERTIES SOURCES "${_lvgl_srcs}")
endif()

add_executable(firmware
  main_device.c
  ${CMAKE_CURRENT_LIST_DIR}/../app/ui.c
  ${CMAKE_CURRENT_LIST_DIR}/../app/proto.c
  ${CMAKE_CURRENT_LIST_DIR}/../firmware/lib/Config/DEV_Config.c
  ${CMAKE_CURRENT_LIST_DIR}/../firmware/lib/LCD/LCD_1in47.c
)

pico_enable_stdio_usb(firmware 1)
pico_enable_stdio_uart(firmware 0)
pico_add_extra_outputs(firmware)

target_link_libraries(firmware
  lvgl
  pico_stdlib
  hardware_spi
  hardware_pwm
  hardware_i2c
)
