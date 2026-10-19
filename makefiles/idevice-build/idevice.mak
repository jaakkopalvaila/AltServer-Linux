# Builds idevice's C API (https://github.com/jkcoxson/idevice) for DevicePairingManager.
#
# idevice's static library also exports libplist-compatible plist_* functions and the Rust standard
# library, which would clash with the vendored libplist. So the library is prelinked into one object
# (ld -r) whose only global symbols are the functions in idevice-api.txt.
#
# Set IDEVICE_FFI_LIB to a libidevice_ffi.a built elsewhere to skip the cargo build, for example
# one from build-idevice-cross.sh for architectures without a musl-hosted Rust toolchain.
# Either cargo build also generates the header, libraries/idevice/ffi/idevice.h.
ROOT_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
include $(ROOT_DIR)/../main.mak

# A failed ld or objcopy must not leave a partial idevice_ffi.o that the next make treats as built.
.DELETE_ON_ERROR:

IDEVICE_FEATURES := $(shell cat $(ROOT_DIR)/idevice-features.txt)
IDEVICE_TARGET_DIR := $(BUILD_DIR)/idevice-target
IDEVICE_FFI_LIB ?= $(IDEVICE_TARGET_DIR)/release/libidevice_ffi.a

idevice_api := $(ROOT_DIR)/idevice-api.txt

# cargo runs on every make, because only cargo knows when the library is out of date: a submodule
# bump, a change to idevice-features.txt or a new toolchain. When nothing changed it leaves the
# library untouched, and make then does not prelink it again.
$(IDEVICE_TARGET_DIR)/release/libidevice_ffi.a: FORCE
	cd $(LIB_DIR)/idevice/ffi && cargo build --release --locked --target-dir $(IDEVICE_TARGET_DIR) --no-default-features --features $(IDEVICE_FEATURES)

$(BUILD_DIR)/idevice_ffi.o: $(IDEVICE_FFI_LIB) $(idevice_api)
	ld -r $(addprefix -u ,$(shell cat $(idevice_api))) -o $@.tmp $(IDEVICE_FFI_LIB)
	objcopy --keep-global-symbols=$(idevice_api) $@.tmp $@
	rm -f $@.tmp

FORCE:
.PHONY : FORCE

clean::
	rm -f $(BUILD_DIR)/idevice_ffi.o
	rm -rf $(IDEVICE_TARGET_DIR)
.PHONY : clean

all :: $(BUILD_DIR)/idevice_ffi.o
.PHONY : all

.DEFAULT_GOAL := all
