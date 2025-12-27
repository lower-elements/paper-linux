################################################################################
#
# directfb2
#
################################################################################

DIRECTFB2_VERSION = 7d4682d0cc092ed2f28c903175d1a0c104e9e9a8
DIRECTFB2_SITE = https://github.com/DirectFB2/DirectFB2.git
DIRECTFB2_SITE_METHOD = git
DIRECTFB2_LICENSE = LGPL-2.1+
DIRECTFB2_LICENSE_FILES = COPYING
DIRECTFB2_INSTALL_STAGING = YES
DIRECTFB2_DEPENDENCIES = host-python3

DIRECTFB2_CONF_OPTS = \
	-Dconstructors=true \
	-Dos=linux \
	-Ddrmkms=false \
	-Dfbdev=$(if $(BR2_PACKAGE_DIRECTFB2_FBDEV),true,false) \
	-Dlinux_input=$(if $(BR2_PACKAGE_DIRECTFB2_LINUXINPUT),true,false) \
	-Dmulti=false \
	-Dmulti-kernel=false \
	-Ddebug-support=false \
	-Dmemcpy-probing=false \
	-Dnetwork=false \
	-Dpiped-stream=false \
	-Dsentinels=false \
	-Dsmooth-scaling=false \
	-Dtext=false \
	-Dtrace=false \
	-Dmmx=false \
	-Dneon=false \
	-Dfluxcomp=$(DIRECTFB2_DIR)/src/core/fluxcomp.py

$(eval $(meson-package))
