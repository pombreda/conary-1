#
# Copyright (c) 2004 Specifix, Inc.
# All rights reserved
#
import recipe
import time
import files
import commit
import os
import util
import sha1helper
import lookaside
import shutil
import types

def cook(repos, cfg, recipeFile, prep=0, macros=()):
    if type(recipeFile) is types.ClassType:
        classList = {recipeFile.__name__: recipeFile}
    else:
        classList = recipe.RecipeLoader(recipeFile)
    built = []

    for (className, recipeClass) in classList.items():
	print "Building", className

	# Find the files and ids which were owned by the last version of
	# this package on the branch. We also construct an object which
	# lets us look for source files this build needs inside of the
	# repository
	fileIdMap = {}
	fullName = cfg.packagenamespace + "/" + recipeClass.name
	lcache = lookaside.RepositoryCache(repos)
	if repos.hasPackage(fullName):
	    for pkgName in repos.getPackageList(fullName):
		pkgSet = repos.getPackageSet(pkgName)
		pkg = pkgSet.getLatestPackage(cfg.defaultbranch)
		for (fileId, path, version) in pkg.fileList():
		    fileIdMap[path] = fileId
		    if path[0] != "/":
			# we might need to retrieve this source file
			# to enable a build, so we need to find the
			# sha1 hash of it since that's how it's indexed
			# in the file store
			filedb = repos.getFileDB(fileId)
			file = filedb.getVersion(version)
			lcache.addFileHash(path, file.sha1())

	ident = IdGen(fileIdMap)

        srcdirs = [ os.path.dirname(recipeClass.filename), cfg.sourcepath % {'pkgname': recipeClass.name} ]
	recipeObj = recipeClass(cfg, lcache, srcdirs, macros)

	builddir = cfg.buildpath + "/" + recipeObj.name

	recipeObj.setup()
	recipeObj.unpackSources(builddir)

        # if we're only extracting, continue to the next recipe class.
        if prep:
            continue
        
        cwd = os.getcwd()
        os.chdir(builddir + '/' + recipeObj.mainDir())
	recipeObj.doBuild(builddir)

	destdir = "/var/tmp/srs/%s-%d" % (recipeObj.name, int(time.time()))
        if os.path.exists(destdir):
            shutil.rmtree(destdir)
        util.mkdirChain(destdir)
	recipeObj.doInstall(builddir, destdir)
        
        os.chdir(cwd)
        
        recipeObj.packages(destdir)
        pkgSet = recipeObj.getPackageSet()

        pkgname = cfg.packagenamespace + "/" + recipeObj.name

	for (name, buildPkg) in pkgSet.packageSet():
            built.append(pkgname + "/" + name)
	    fileList = []

	    for filePath in buildPkg.keys():
		realPath = destdir + filePath
		f = files.FileFromFilesystem(realPath, ident(filePath))
		fileList.append((f, realPath, filePath))

	    commit.finalCommit(repos, cfg, pkgname + "/" + name,
                               recipeObj.version, fileList)

        # XXX include recipe files loaded by a recipe to derive
	recipeName = os.path.basename(recipeClass.filename)
	f = files.FileFromFilesystem(recipeClass.filename, ident(recipeName),
                                     type = "src")
	fileList = [ (f, recipeClass.filename, recipeName) ]

	for file in recipeObj.allSources():
            src = lookaside.findAll(cfg, lcache, file, recipeObj.name, srcdirs)
	    srcName = os.path.basename(src)
	    f = files.FileFromFilesystem(src, ident(srcName), type = "src")
	    fileList.append((f, src, srcName))

	commit.finalCommit(repos, cfg, pkgname + "/sources",
			   recipeObj.version, fileList)

	recipeObj.cleanup(builddir, destdir)
    return built

class IdGen:

    def __call__(self, path):
	if self.map.has_key(path):
	    return self.map[path]

	return sha1helper.hashString("%s %f %s" % (path, time.time(), 
						    self.noise))

    def __init__(self, map):
	# file ids need to be unique. we include the time and path when
	# we generate them; any data put here is also used
	uname = os.uname()
	self.noise = "%s %s" % (uname[1], uname[2])
	self.map = map
