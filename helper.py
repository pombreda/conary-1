#
# Copyright (c) 2004 Specifix, Inc.
# All rights reserved
#

"""
Simple functions used throughout srs.
"""

import repository
import versions

def findPackage(repos, packageNamespace, defaultNick, name, 
		versionStr = None, forceGroup = 0, oneMatch = 1):
    """
    Looks up a package in the given repository based on the name and
    version provided. If any errors are occured, PackageNotFound is
    raised with an appropriate error message. Multiple matches could
    be found if versionStr refers to a branch nickname.

    @param repos: Repository to look for the package in
    @type repos: repository.Repository
    @param packageNamespace: Default namespace for the package
    @type packageNamespace: str
    @param defaultNick: Nickname of the branch to use if no branch
    is specified
    @type defaultNick: versions.BranchName
    @param name: Package name
    @type name: str
    @param versionStr: Package version
    @type versionStr: str
    @param forceGroup: If true the name should specify a group
    @type forceGroup: boolean
    @rtype: list of package.Package
    """

    if name[0] != ":":
	name = packageNamespace + ":" + name
    else:
	name = name

    if not repos.hasPackage(name):
	raise PackageNotFound, "package %s does not exist" % name

    if forceGroup:
	if name.count(":") != 2:
	    raise PackageNotFound, "group names may not include colons"

	last = name.split(":")[-1]
	if not last.startswith("group-"):
	    raise PackageNotFound,  \
		    "only groups may be checked out of the repository"

    if not defaultNick:
	if versionStr[0] != "/" and (versionStr.find("/") != -1 or
				     versionStr.find("@") == -1):
	    raise PackageNotFound, \
		"fully qualified version or branch nickname " + \
		"expected instead of %s" % versionStr

    # a version is a branch nickname if
    #   1. it doesn't being with / (it isn't fully qualified)
    #   2. it only has one element (no /)
    #   3. it contains an @ sign
    if not versionStr or (versionStr[0] != "/" and  \
	# branch nickname was given
	    (versionStr.find("/") == -1) and versionStr.count("@")):
	if versionStr[0] == "@":
	    versionStr = packageNamespace[1:] + versionStr

	if versionStr:
	    try:
		nick = versions.BranchName(versionStr)
	    except versions.ParseError:
		raise repository.PackageMissing, "invalid version %s" % versionStr
	else:
	    nick = defaultNick

	branchList = repos.getPackageNickList(name, nick)
	if not branchList:
	    raise PackageNotFound, "branch %s does not exist for package %s" \
			% (str(nick), name)

	pkgList = []
	for branch in branchList:
	    pkgList.append(repos.getLatestPackage(name, branch))
    elif versionStr[0] != "/" and versionStr.find("/") == -1:
	# version/release was given
	branchList = repos.getPackageNickList(name, defaultNick)
	if not branchList:
	    raise PackageNotFound, \
			"branch %s does not exist for package %s" \
			% (str(defaultNick), name)
	
	try:
	    verRel = versions.VersionRelease(versionStr)
	except versions.ParseError, e:
	    raise PackageNotFound, str(e)

	pkgList = []
	for branch in branchList:
	    version = branch.copy()
	    version.appendVersionReleaseObject(verRel)
	    try:
		pkg = repos.getPackageVersion(name, version)
		pkgList.append(pkg)
	    except repository.PackageMissing, e:
		pass

	if not pkgList:
	    raise PackageNotFound, \
		"version %s of %s is not on any branch named %s" % \
		(versionStr, name, str(defaultNick))
    elif versionStr[0] != "/":
	# partial version string, we don't support this
	raise PackageNotFound, \
	    "incomplete version string %s not allowed" % versionStr
    else:
	try:
	    version = versions.VersionFromString(versionStr)
	except versions.ParseError:
	    raise PackageNotFound, str(e)

	try:
	    if version.isBranch():
		pkg = repos.getLatestPackage(name, version)
	    else:
		pkg = repos.getPackageVersion(name, version)
	except repository.PackageMissing, e:  
	    raise PackageNotFound, str(e)

	pkgList = [ pkg ]

    return pkgList

class PackageNotFound(Exception):

    def __str__(self):
	return self.msg

    def __init__(self, str):
	self.msg = str
