################################################################################
#
# peugeot-carberry
#
################################################################################

PEUGEOT_CARBERRY_VERSION = 1.5-peugeot1
PEUGEOT_CARBERRY_SITE = $(TOPDIR)/package/peugeot-carberry/vendor
PEUGEOT_CARBERRY_SITE_METHOD = local

# Buildroot intentionally does not run the normal patch stage for
# SITE_METHOD=local packages. Apply our Peugeot-specific modification
# to the rsynced build copy, while keeping vendor/carberry.c as a clean
# upstream-derived baseline.
define PEUGEOT_CARBERRY_APPLY_LOCAL_PATCHES
	$(APPLY_PATCHES) $(@D) $(PEUGEOT_CARBERRY_PKGDIR) 0001-carberryd-bind-loopback-only.patch
endef

PEUGEOT_CARBERRY_POST_RSYNC_HOOKS += PEUGEOT_CARBERRY_APPLY_LOCAL_PATCHES

define PEUGEOT_CARBERRY_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) \
		-I$(@D) \
		-o $(@D)/carberryd \
		$(@D)/carberry.c \
		$(TARGET_LDFLAGS)

	$(TARGET_CC) $(TARGET_CFLAGS) \
		-o $(@D)/carberry-boot-led \
		$(@D)/carberry-boot-led.c \
		$(TARGET_LDFLAGS)
endef

define PEUGEOT_CARBERRY_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 \
		$(@D)/carberryd \
		$(TARGET_DIR)/usr/sbin/carberryd

	$(INSTALL) -D -m 0755 \
		$(@D)/carberry-boot-led \
		$(TARGET_DIR)/usr/sbin/carberry-boot-led
endef

$(eval $(generic-package))
