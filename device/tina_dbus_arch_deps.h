/* SPDX-License-Identifier: AFL-2.0 OR GPL-2.0-or-later */
/* Generated-equivalent D-Bus architecture definitions for Tina RV32 musl. */
#if !defined(DBUS_INSIDE_DBUS_H) && !defined(DBUS_COMPILATION)
#error "Only <dbus/dbus.h> can include this file"
#endif

#ifndef DBUS_ARCH_DEPS_H
#define DBUS_ARCH_DEPS_H

#include <dbus/dbus-macros.h>

DBUS_BEGIN_DECLS

#define DBUS_HAVE_INT64 1
_DBUS_GNUC_EXTENSION typedef long long dbus_int64_t;
_DBUS_GNUC_EXTENSION typedef unsigned long long dbus_uint64_t;
#define DBUS_INT64_MODIFIER "ll"
#define DBUS_INT64_CONSTANT(value) (_DBUS_GNUC_EXTENSION(value##LL))
#define DBUS_UINT64_CONSTANT(value) (_DBUS_GNUC_EXTENSION(value##ULL))

typedef int dbus_int32_t;
typedef unsigned int dbus_uint32_t;
typedef short dbus_int16_t;
typedef unsigned short dbus_uint16_t;

#define DBUS_SIZEOF_VOID_P 4
#define DBUS_MAJOR_VERSION 1
#define DBUS_MINOR_VERSION 12
#define DBUS_MICRO_VERSION 12
#define DBUS_VERSION_STRING "1.12.12"
#define DBUS_VERSION ((1 << 16) | (12 << 8) | 12)

DBUS_END_DECLS

#endif
