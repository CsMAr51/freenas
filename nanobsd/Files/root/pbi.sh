#!/bin/sh

pbi_add=/usr/local/sbin/pbi_add
pbi_info=/usr/local/sbin/pbi_info
pbi_create=/usr/local/sbin/pbi_create

backupdir=/mnt/plugins/pbibackup
logfile=/tmp/pbi.log

usage()
{
	echo "$(basename $0) <pbibackup|pbirestore|jailupdate> [arguments] ..."
	exit 1
}

get_plugins_path()
{
	. /etc/rc.freenas

	${FREENAS_SQLITE_CMD} ${FREENAS_CONFIG} "
	SELECT
		plugins_path
	FROM
		services_pluginsjail
	"
}

get_plugins_jail_name()
{
	. /etc/rc.freenas

	${FREENAS_SQLITE_CMD} ${FREENAS_CONFIG} "
	SELECT
		jail_name
	FROM
		services_pluginsjail
	"
}

get_plugins_jail_path()
{
	. /etc/rc.freenas

	${FREENAS_SQLITE_CMD} ${FREENAS_CONFIG} "
	SELECT
		jail_path || '/' || jail_name
	FROM
		services_pluginsjail
	"
}

jail_stop()
{
	. /etc/rc.freenas

	local jail=$(get_plugins_jail_name)
	if [ -z "${jail}" ]
	then
		return 1
	fi

	/etc/rc.d/jail stop ${jail}
	return $?
}

jail_status()
{
	. /etc/rc.freenas

	local jail="$(get_plugins_jail_name)"
	if [ -z "${jail}" ]
	then
		return 1
	fi

	local jid="$(jls|awk "\$3 ~ /${jailname}/"|awk '{ print $1 }')"
	if [ -z "${jid}" ]
	then
		return 1
	fi

	return 0
}

jail_start()
{
	. /etc/rc.freenas

	local jail=$(get_plugins_jail_name)
	if [ -z "${jail}" ]
	then
		return 1
	fi

	/etc/rc.d/ix-jail start
	/etc/rc.d/jail start ${jail}
	return $?
}

jail_context()
{
	local op="$1"
	shift
	local args="$*"

	#
	# Bail if we're already running within jail context.
	#
	if [ "${JAIL_CONTEXT}" = "1" ]
	then
		return 0
	fi

	local jailpath=$(get_plugins_jail_path)
	if [ -z "${jailpath}" -o ! -d "${jailpath}" ]
	then
		return 1
	fi

	#
	# /root inside the jail is where we copy ourselves.
	#
	local root=${jailpath}/root
	if [ -z "${root}" -o ! -d "${root}" ]
	then
		return 1
	fi

	local self="$(basename $0)"
	local this="${root}/${self}"
	local script="/root/${self}"

	#
	# Do the copy and fail if not successful, otherwise everything else borks.
	#
	cp "$0" "${root}"
	if [ -z "${this}" ]
	then
		return 1
	fi

	local jailname=$(get_plugins_jail_name)
	if [ -z "${jailname}" ]
	then
		return 1
	fi

	#
	# Get the jail id using the jail name we just got.
	#
	local jid=$(jls|awk "\$3 ~ /${jailname}/"|awk '{ print $1 }')
	if [ -z "${jid}" ]
	then
		return 1
	fi

	#
	# Setup environment variable so we know if we're running inside the jail
	#
	export JAIL_CONTEXT=1

	#
	# Now run the command within the jail!
	#
	jexec "${jid}" "${script}" "${op}" "${args}"


	#
	# We've done our business, now clean up!
	#
	unset JAIL_CONTEXT
	return $?
}

in_jail_context()
{
	if [ "${JAIL_CONTEXT}" = "1" ]
	then
		return 0
	fi

	return 1
}

pbi_backup()
{
	local pbis="$*"
	local ret=0

	jail_context pbibackup "${pbis}" >> "${logfile}" 2>&1
	if [ "$?" != "0" ]
	then
		return 1
	fi

	if ! in_jail_context
	then
		return 0
	fi

	if [ -z "${pbis}" ]
	then
		pbis="$(${pbi_info})"
	fi

	mkdir -p ${backupdir}
	for pbi in ${pbis}
	do
		${pbi_create} -o "${backupdir}" -b "${pbi}"
		if [ "$?" != "0" ]
		then
			ret=1
		fi
	done

	return ${ret}
}

pbi_restore()
{
	local pbis="$*"
	local ret=0

	jail_context pbirestore "${pbis}" >> "${logfile}" 2>&1
	if [ "$?" != "0" ]
	then
		return 1
	fi

	if ! in_jail_context
	then
		return 0
	fi

	if [ -z "${pbis}" ]
	then
		pbis=$(ls ${backupdir}/*.pbi 2>&1)
	fi

	for pbi in ${pbis}
	do
		${pbi_add} -f --no-checksig "${pbi}"
		if [ "$?" != "0" ]
		then
			ret=1
		fi
	done

	return ${ret}
}

jail_update()
{
	local pbi="${1}"
	if [ -z "${pbi}" -o ! -f "${pbi}" ]
	then
		return 1
	fi

	local jailpath=$(get_plugins_jail_path)
	if [ -z "${jailpath}" -o ! -d "${jailpath}" ]
	then
		return 1
	fi

	jail_status
	local on="$?"
	if [ "${on}" = "0" ]
	then
		jail_stop
	fi

	#
	# Secret voodoo to coerce pbi_add into doing exactly what we want,
	# how we want, and WHERE we want... muahahah!
	#
	export PBI_ALTEXTRACT_PATH="${jailpath}"
	${pbi_add} -e -f --no-checksig "${pbi}" >> "${logfile}" 2>&1

	if [ "${on}" = "0" ]
	then
		jail_start
	fi

	return $?
}


main()
{
	if [ $# -lt 1 ]
	then
		usage
	fi

	local op="$1"
	shift
	local args="$*"

	: > "${logfile}"

	op=$(echo ${op}|tr A-Z a-z)
	case ${op} in
		pbibackup)
			pbi_backup ${args}
			;;
		pbirestore)
			pbi_restore ${args}
			;;
		jailupdate)
			jail_update ${args}
			;;
		*)
			usage
			;;
	esac
}


main $*
