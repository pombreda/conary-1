#
# Copyright (c) 2004 Specifix, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.opensource.org/licenses/cpl.php.
#
# This program is distributed in the hope that it will be useful, but
# without any waranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.
#
import md5
import sqlite3
import re

class NetworkAuthorization:
    def check(self, authToken, write = False, admin = False, label = None, trove = None):
        if label and label.getHost() != self.name:
            raise RepositoryMismatch

        if not write and not admin and self.anonReads:
            return True

        if not authToken[0]:
            return False

        stmt = """
            SELECT item FROM
               (SELECT userId as uuserId FROM Users WHERE user=? AND
                    password=?)
            JOIN Permissions ON uuserId=Permissions.userId
            LEFT OUTER JOIN Items ON Permissions.itemId = Items.itemId
        """
        m = md5.new()
        m.update(authToken[1])
        params = [authToken[0], m.hexdigest()]

        where = []
        if label:
            where.append(" labelId=(SELECT labelId FROM Labels WHERE " \
                            "label=?) OR labelId is Null")
            params.append(label.asString())

        if write:
            where.append("write=1")

        if admin:
            where.append("admin=1")

        if where:
            stmt += "WHERE " + " AND ".join(where)

        cu = self.db.cursor()
        cu.execute(stmt, params)

        for (troveName, ) in cu:
            if not troveName or not trove:
                return True

            regExp = self.reCache.get(troveName, None)
            if regExp is None:
                regExp = re.compile(troveName)
                self.reCache[troveName] = regExp

            if regExp.match(trove):
                return True

        return False
        
    def checkUserPass(self, authToken, label = None):
        if label and label.getHost() != self.name:
            raise RepositoryMismatch

        stmt = "SELECT COUNT(userId) FROM Users WHERE user=? AND password=?"
        m = md5.new()
        m.update(authToken[1])
        cu = self.db.cursor()
        cu.execute(stmt, authToken[0], m.hexdigest())

        row = cu.fetchone()
        return row[0]

    def add(self, user, password, write=True, admin=False):
        cu = self.db.cursor()
        
        m = md5.new()
        m.update(password)
        cu.execute("INSERT INTO Users VALUES (Null, ?, ?)", user, m.hexdigest())
        userId = cu.lastrowid

        cu.execute("INSERT INTO Permissions VALUES (?, Null, Null, ?, ?)",
                   userId, write, admin)
        self.db.commit()

    def changePassword(self, user, newPassword):
        cu = self.db.cursor()
        
        m = md5.new()
        m.update(newPassword)
        password = m.hexdigest()

        cu.execute("UPDATE Users SET password=? WHERE user=?", password, user)
        self.db.commit()

    def iterUsers(self):
        cu = self.db.cursor()
        cu.execute("""SELECT Users.user, Users.userId, Permissions.write, Permissions.admin FROM Users
                      LEFT JOIN Permissions ON Users.userId=Permissions.userId""")
        for row in cu:
            yield row

    def __init__(self, db, name, anonymousReads = False):
        self.name = name
        self.db = db
        self.anonReads = anonymousReads
        self.reCache = {}

        cu = self.db.cursor()
        cu.execute("SELECT tbl_name FROM sqlite_master WHERE type='table'")
        tables = [ x[0] for x in cu ]

        commit = False
        
        if "Users" not in tables:
            cu.execute("""CREATE TABLE Users (userId INTEGER PRIMARY KEY,
                                              user STRING UNIQUE,
                                              password STRING)""")
            commit = True
        if "Permissions" not in tables:
            cu.execute("""CREATE TABLE Permissions (userId INTEGER,
                                                    labelId INTEGER,
                                                    itemId INTEGER,
                                                    write INTEGER,
                                                    admin INTEGER)""")
            cu.execute("""CREATE INDEX PermissionsIdx
                          ON Permissions(userId, labelId, itemId)""")
            commit = True

        if commit:
            self.db.commit()

class RepositoryMismatch(Exception):
    pass

class InsufficientPermission(Exception):
    pass
