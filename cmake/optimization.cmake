# Optional release optimizations: link-time optimization (LTO) and profile-guided optimization (PGO).
#
#   -DSC_LTO=ON           whole-program optimization (MSVC /GL + /LTCG, GCC -flto=auto, Clang -flto=thin)
#   -DSC_PGO=GENERATE     instrumented build; run simc on representative profiles to record a profile
#   -DSC_PGO=USE          optimized build using the recorded profile
#   -DSC_PGO_PROFILE=...  Clang: merged .profdata file (default <build>/pgo/simc.profdata)
#                         GCC:   profile directory      (default <build>/pgo)
#                         MSVC:  unused, the .pgd/.pgc files stay next to simc.exe in the build directory
#
# tools/pgo_build.py runs the whole GENERATE -> train -> USE cycle. These options do not change simulation
# results (no fast-math or similar), they only make the binary faster.
#
# Clang LTO needs an LTO-capable linker, e.g. -DCMAKE_EXE_LINKER_FLAGS=-fuse-ld=lld.

option(SC_LTO "Enable link-time optimization for the simc engine and cli" OFF)
set(SC_PGO "OFF" CACHE STRING "Profile-guided optimization phase: OFF, GENERATE or USE")
set_property(CACHE SC_PGO PROPERTY STRINGS OFF GENERATE USE)
set(SC_PGO_PROFILE "" CACHE PATH "Profile data used by SC_PGO=USE (see cmake/optimization.cmake)")

string(TOUPPER "${SC_PGO}" SC_PGO_PHASE)
if(NOT SC_PGO_PHASE MATCHES "^(OFF|GENERATE|USE)$")
  message(FATAL_ERROR "SC_PGO must be OFF, GENERATE or USE (got '${SC_PGO}')")
endif()

# Apply the optimizations to the engine library and the cli executable. Compile options are private; link
# options are added to the engine's interface so every executable linking the engine (cli, gui, tests) links
# correctly against LTO/instrumented objects.
function(sc_apply_optimizations engine_target exe_target)
  set(not_debug "$<NOT:$<CONFIG:Debug>>")

  if(MSVC)
    # MSVC PGO requires whole-program optimization, so both options share /GL + /LTCG.
    if(NOT SC_LTO AND SC_PGO_PHASE STREQUAL "OFF")
      return()
    endif()
    target_compile_options(${engine_target} PRIVATE "$<${not_debug}:/GL;/Gw;/Gy;/Ob3>")
    target_compile_options(${exe_target} PRIVATE "$<${not_debug}:/GL;/Gw;/Gy;/Ob3>")
    set_property(TARGET ${engine_target} APPEND PROPERTY STATIC_LIBRARY_OPTIONS "$<${not_debug}:/LTCG>")
    if(SC_PGO_PHASE STREQUAL "GENERATE")
      target_link_options(${exe_target} PRIVATE "$<${not_debug}:/LTCG:PGINSTRUMENT>")
    elseif(SC_PGO_PHASE STREQUAL "USE")
      target_link_options(${exe_target} PRIVATE "$<${not_debug}:/LTCG:PGOPTIMIZE>")
    else()
      target_link_options(${exe_target} PRIVATE "$<${not_debug}:/LTCG>")
    endif()
    return()
  endif()

  set(compile_flags)
  set(link_flags)

  if(SC_LTO)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
      list(APPEND compile_flags -flto=thin)
      list(APPEND link_flags -flto=thin)
    elseif(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
      list(APPEND compile_flags -flto=auto)
      list(APPEND link_flags -flto=auto)
    else()
      message(WARNING "SC_LTO is not supported for compiler ${CMAKE_CXX_COMPILER_ID}, ignoring it")
    endif()
  endif()

  if(NOT SC_PGO_PHASE STREQUAL "OFF")
    set(profile "${SC_PGO_PROFILE}")
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
      if(SC_PGO_PHASE STREQUAL "GENERATE")
        list(APPEND compile_flags -fprofile-instr-generate)
        list(APPEND link_flags -fprofile-instr-generate)
      else()
        if(NOT profile)
          set(profile "${CMAKE_BINARY_DIR}/pgo/simc.profdata")
        endif()
        list(APPEND compile_flags "-fprofile-instr-use=${profile}" -Wno-profile-instr-out-of-date
             -Wno-profile-instr-unprofiled)
      endif()
    elseif(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
      if(NOT profile)
        set(profile "${CMAKE_BINARY_DIR}/pgo")
      endif()
      if(SC_PGO_PHASE STREQUAL "GENERATE")
        # simc is multithreaded: atomic counter updates keep the recorded profile consistent
        list(APPEND compile_flags "-fprofile-generate=${profile}" -fprofile-update=atomic)
        list(APPEND link_flags "-fprofile-generate=${profile}")
      else()
        list(APPEND compile_flags "-fprofile-use=${profile}" -fprofile-partial-training -Wno-missing-profile)
      endif()
    else()
      message(WARNING "SC_PGO is not supported for compiler ${CMAKE_CXX_COMPILER_ID}, ignoring it")
    endif()
  endif()

  if(compile_flags)
    target_compile_options(${engine_target} PRIVATE "$<${not_debug}:${compile_flags}>")
    target_compile_options(${exe_target} PRIVATE "$<${not_debug}:${compile_flags}>")
  endif()
  if(link_flags)
    target_link_options(${engine_target} INTERFACE "$<${not_debug}:${link_flags}>")
  endif()
endfunction()
