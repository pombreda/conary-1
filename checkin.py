#
# Copyright (c) 2004 Specifix, Inc.
# All rights reserved
#

from build import recipe, lookaside
from local import update
from repository import changeset
import cook
import files
import helper
import log
import magic
import os
import package
import repository
import sys
import util
import versions

class SourceState(package.Package):

    def removeFilePath(self, file):
	for (fileId, path, version) in self.iterFileList():
	    if path == file: 
		self.removeFile(fileId)
		return True

	return False

    def write(self, filename):
	f = open(filename, "w")
	f.write("name %s\n" % self.name)
	if self.version:
	    f.write("version %s\n" % self.version.asString())
	f.write(self.freezeFileList())

    def changeBranch(self, branch):
	self.branch = branch

    def getRecipeFileName(self):
        # XXX this is not the correct way to solve this problem
        # assumes a fully qualified trove name
        name = self.getName().split(':')[0]
        return os.path.join(os.getcwd(), name + '.recipe')

    def expandVersionStr(self, versionStr):
	if versionStr[0] == "@":
	    # get the name of the repository from the current branch
	    repName = self.getVersion().branch().label().getHost()
	    return repName + versionStr
	elif versionStr[0] != "/" and versionStr.find("@") == -1:
	    # non fully-qualified version; make it relative to the current
	    # branch
	    return self.getVersion().branch().asString() + "/" + versionStr

	return versionStr

    def __init__(self, name, version):
	package.Package.__init__(self, name, version, None)

class SourceStateFromFile(SourceState):

    def parseFile(self, filename):
	f = open(filename)
	rc = [self]
	for (what, isBranch) in [ ('name', 0), ('version', 1) ]:
	    line = f.readline()
	    fields = line.split()
	    assert(len(fields) == 2)
	    assert(fields[0] == what)
	    if isBranch:
		rc.append(versions.VersionFromString(fields[1]))
	    else:
		rc.append(fields[1])

	SourceState.__init__(*rc)

	self.readFileList(f)

    def __init__(self, file):
	if not os.path.isfile(file):
	    log.error("SRS file must exist in the current directory for source commands")
	    raise OSError  # XXX

	self.parseFile(file)

def _verifyAtHead(repos, headPkg, state):
    headVersion = repos.getTroveLatestVersion(state.getName(), 
					 state.getVersion().branch())
    if not headVersion == state.getVersion():
	return False

    # make sure the files in this directory are based on the same
    # versions as those in the package at head
    for (fileId, path, version) in state.iterFileList():
	if isinstance(version, versions.NewVersion):
	    assert(not headPkg.hasFile(fileId))
	    # new file, it shouldn't be in the old package at all
	else:
	    srcFileVersion = headPkg.getFile(fileId)[1]
	    if not version == srcFileVersion:
		return False

    return True

def _getRecipeLoader(cfg, repos, recipeFile):
    # load the recipe; we need this to figure out what version we're building
    try:
        loader = recipe.RecipeLoader(recipeFile, cfg=cfg, repos=repos)
    except recipe.RecipeFileError, e:
	log.error("unable to load recipe file %s: %s", recipeFile, str(e))
        return None
    except IOError, e:
	log.error("unable to load recipe file %s: %s", recipeFile, e.strerror)
        return None
    
    if not loader:
	log.error("unable to load a valid recipe class from %s", recipeFile)
	return None

    return loader


def checkout(repos, cfg, workDir, name, versionStr = None):
    # We have to be careful with labels
    name += ":source"
    try:
        trvList = repos.findTrove(cfg.installLabel, name, None,
				  versionStr = versionStr)
    except repository.repository.PackageNotFound, e:
        log.error(str(e))
        return
    if len(trvList) > 1:
	log.error("branch %s matches more then one version", versionStr)
	return
    trv = trvList[0]
	
    if not workDir:
	workDir = trv.getName().split(":")[0]

    if not os.path.isdir(workDir):
	try:
	    os.mkdir(workDir)
	except OSError, err:
	    log.error("cannot create directory %s/%s: %s", os.getcwd(),
                      workDir, str(err))
	    return

    branch = helper.fullBranchName(cfg.installLabel, trv.getVersion(), 
				   versionStr)
    state = SourceState(trv.getName(), trv.getVersion())

    # it's a shame that findTrove already sent us the trove since we're
    # just going to request it again
    cs = repos.createChangeSet([(trv.getName(), None, None, trv.getVersion(),
			        True)])

    pkgCs = cs.iterNewPackageList().next()

    fileList = pkgCs.getNewFileList()
    fileList.sort()

    for (fileId, path, version) in fileList:
	fullPath = workDir + "/" + path
	fileObj = files.ThawFile(cs.getFileChange(fileId), fileId)
	if fileObj.hasContents:
	    contents = cs.getFileContents(fileId)[1]
	else:
	    contents = None

	fileObj.restore(contents, fullPath, 1)

	state.addFile(fileId, path, version)

    state.write(workDir + "/SRS")

def commit(repos, cfg):
    try:
        state = SourceStateFromFile("SRS")
    except OSError:
        return

    if isinstance(state.getVersion(), versions.NewVersion):
	# new package, so it shouldn't exist yet
	if repos.hasPackage(state.getName()):
	    log.error("%s is marked as a new package but it " 
		      "already exists" % state.getName())
	    return
	srcPkg = None
    else:
	srcPkg = repos.getTrove(state.getName(), state.getVersion(), None)

	if not _verifyAtHead(repos, srcPkg, state):
	    log.error("contents of working directory are not all "
		      "from the head of the branch; use update")
	    return

    loader = _getRecipeLoader(cfg, repos, state.getRecipeFileName())
    if loader is None: return

    # fetch all the sources
    recipeClass = loader.getRecipe()
    if issubclass(recipeClass, recipe.PackageRecipe):
        lcache = lookaside.RepositoryCache(repos)
        srcdirs = [ os.path.dirname(recipeClass.filename),
                    cfg.sourcePath % {'pkgname': recipeClass.name} ]
        recipeObj = recipeClass(cfg, lcache, srcdirs)
        recipeObj.setup()
        files = recipeObj.fetchAllSources()
    
    recipeVersionStr = recipeClass.version

    if isinstance(state.getVersion(), versions.NewVersion):
	branch = versions.Version([cfg.buildLabel])
    else:
	branch = state.getVersion().branch()

    newVersion = helper.nextVersion(repos, state.getName(), recipeVersionStr, 
				    None, branch, binary = False)

    result = update.buildLocalChanges(repos, [(state, srcPkg, newVersion)],
				      flags = update.IGNOREUGIDS)
    if not result: return

    (changeSet, ((isDifferent, newState),)) = result

    if not isDifferent:
	log.info("no changes have been made to commit")
    else:
	repos.commitChangeSet(changeSet)
	newState.write("SRS")

def diff(repos, versionStr = None):
    try:
        state = SourceStateFromFile("SRS")
    except OSError:
        return

    if state.getVersion() == versions.NewVersion():
	log.error("no versions have been committed")
	return

    if versionStr:
	versionStr = state.expandVersionStr(versionStr)

	pkgList = repos.findTrove(None, state.getName(), None, None, 
				  versionStr = versionStr)
	if len(pkgList) > 1:
	    log.error("%s specifies multiple versions" % versionStr)
	    return

	oldPackage = pkgList[0]
    else:
	oldPackage = repos.getTrove(state.getName(), state.getVersion(), None)

    result = update.buildLocalChanges(repos, [(state, oldPackage, 
					       versions.NewVersion())],
				      flags = update.IGNOREUGIDS)
    if not result: return

    (changeSet, ((isDifferent, newState),)) = result
    if not isDifferent: return

    packageChanges = changeSet.iterNewPackageList()
    pkgCs = packageChanges.next()
    assert(util.assertIteratorAtEnd(packageChanges))

    for (fileId, path, newVersion) in pkgCs.getNewFileList():
	print "%s: new" % path

    for (fileId, path, newVersion) in pkgCs.getChangedFileList():
	if path:
	    oldPath = oldPackage.getFile(fileId)[0]
	    dispStr = "%s (aka %s)" % (path, oldPath)
	else:
	    path = oldPackage.getFile(fileId)[0]
	    dispStr = path
	
	if not newVersion:
	    sys.stdout.write(dispStr + '\n')
	    continue
	    
	sys.stdout.write(dispStr + ": changed\n")
	sys.stdout.write("Index: %s\n%s\n" %(path, '=' * 68))

	csInfo = changeSet.getFileChange(fileId)
	sys.stdout.write('\n'.join(files.fieldsChanged(csInfo)))

	if files.contentsChanged(csInfo):
	    (contType, contents) = changeSet.getFileContents(fileId)
	    if contType == changeset.ChangedFileTypes.diff:
		lines = contents.get().readlines()
		str = "".join(lines)
		print
		print str
		print

    for fileId in pkgCs.getOldFileList():
	path = oldPackage.getFile(fileId)[0]
	print "%s: removed" % path
	
def updateSrc(repos, versionStr = None):
    try:
        state = SourceStateFromFile("SRS")
    except OSError:
        return
    pkgName = state.getName()
    baseVersion = state.getVersion()
    
    if not versionStr:
	headVersion = repos.getTroveLatestVersion(pkgName, 
						  state.getVersion().branch())
	head = repos.getTrove(pkgName, headVersion, None)
	newBranch = None
	headVersion = head.getVersion()
	if headVersion == baseVersion:
	    log.info("working directory is already based on head of branch")
	    return
    else:
	versionStr = state.expandVersionStr(versionStr)

	pkgList = repos.findTrove(None, pkgName, None, None, 
				  versionStr = versionStr)
	if len(pkgList) > 1:
	    log.error("%s specifies multiple versions" % versionStr)
	    return

	head = pkgList[0]
	headVersion = head.getVersion()
	newBranch = helper.fullBranchName(None, headVersion, versionStr)

    changeSet = repos.createChangeSet([(pkgName, None, baseVersion, 
					headVersion, 0)])

    packageChanges = changeSet.iterNewPackageList()
    pkgCs = packageChanges.next()
    assert(util.assertIteratorAtEnd(packageChanges))

    fsJob = update.FilesystemJob(repos, changeSet, 
				 { state.getName() : state }, "",
				 flags = update.IGNOREUGIDS | update.MERGE)
    errList = fsJob.getErrorList()
    if errList:
	for err in errList: log.error(err)
    fsJob.apply()
    newPkgs = fsJob.iterNewPackageList()
    newState = newPkgs.next()
    assert(util.assertIteratorAtEnd(newPkgs))

    if newState.getVersion() == pkgCs.getNewVersion() and newBranch:
	newState.changeBranch(newBranch)

    newState.write("SRS")

def addFile(file):
    try:
        state = SourceStateFromFile("SRS")
    except OSError:
        return

    try:
	os.lstat(file)
    except OSError:
	log.error("files must be created before they can be added")
	return

    for (fileId, path, version) in state.iterFileList():
	if path == file:
	    log.error("file %s is already part of this source component" % path)
	    return

    fileMagic = magic.magic(file)
    if fileMagic and fileMagic.name == "changeset":
	log.error("do not add changesets to source components")
	return

    fileId = cook.makeFileId(os.getcwd(), file)

    state.addFile(fileId, file, versions.NewVersion())
    state.write("SRS")

def removeFile(file):
    try:
        state = SourceStateFromFile("SRS")
    except OSError:
        return

    if not state.removeFilePath(file):
	log.error("file %s is not under management" % file)

    if os.path.exists(file):
	os.unlink(file)

    state.write("SRS")

def newPackage(repos, cfg, name):
    name += ":source"

    state = SourceState(name, versions.NewVersion())

    if repos and repos.hasPackage(name):
	log.error("package %s already exists" % name)
	return

    dir = name.split(":")[0]
    if not os.path.isdir(dir):
	try:
	    os.mkdir(dir)
	except:
	    log.error("cannot create directory %s/%s", os.getcwd(), dir)
	    return

    state.write(dir + "/" + "SRS")

def renameFile(oldName, newName):
    try:
        state = SourceStateFromFile("SRS")
    except OSError:
        return

    if not os.path.exists(oldName):
	log.error("%s does not exist or is not a regular file" % oldName)
	return

    try:
	os.lstat(newName)
    except:
	pass
    else:
	log.error("%s already exists" % newName)
	return

    for (fileId, path, version) in state.iterFileList():
	if path == oldName:
	    os.rename(oldName, newName)
	    state.addFile(fileId, newName, version)
	    state.write("SRS")
	    return
    
    log.error("file %s is not under management" % oldName)
