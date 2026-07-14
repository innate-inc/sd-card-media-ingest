cmake_minimum_required(VERSION 3.13)
project(ingest_sim C)
set(CMAKE_C_STANDARD 11)

# LVGL source tree (provided by Nix or -DLVGL_DIR=...).
if(NOT DEFINED LVGL_DIR)
  set(LVGL_DIR $ENV{LVGL_DIR})
endif()
if(NOT LVGL_DIR)
  message(FATAL_ERROR "set -DLVGL_DIR=/path/to/lvgl")
endif()

find_package(PkgConfig REQUIRED)
pkg_check_modules(SDL2 REQUIRED sdl2)
find_package(Threads REQUIRED)

# lv_conf.h lives in ../app; select the SDL backend for the simulator.
include_directories(${CMAKE_CURRENT_SOURCE_DIR}/../app ${SDL2_INCLUDE_DIRS})
link_directories(${SDL2_LIBRARY_DIRS})
add_compile_definitions(LV_CONF_INCLUDE_SIMPLE LV_SIM)

# EXCLUDE_FROM_ALL so LVGL's own install() rules (which want to install a
# lv_conf.h we keep elsewhere) don't run; we only install our binary.
add_subdirectory(${LVGL_DIR} lvgl_build EXCLUDE_FROM_ALL)

# LVGL adds hand-written NEON/Helium .S files to the target unconditionally;
# they can't assemble on x86. Drop them (we use LV_DRAW_SW_ASM_NONE anyway).
get_target_property(_lvgl_srcs lvgl SOURCES)
if(_lvgl_srcs)
  list(FILTER _lvgl_srcs EXCLUDE REGEX "(neon|helium)")
  set_target_properties(lvgl PROPERTIES SOURCES "${_lvgl_srcs}")
endif()

add_executable(ingest-sim
  main_sim.c
  ${CMAKE_CURRENT_SOURCE_DIR}/../app/ui.c
  ${CMAKE_CURRENT_SOURCE_DIR}/../app/proto.c
)
target_link_libraries(ingest-sim lvgl ${SDL2_LIBRARIES} Threads::Threads m)

install(TARGETS ingest-sim RUNTIME DESTINATION bin)
