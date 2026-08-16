-- Separate organization membership from scoped administrative and unit roles.

CREATE TABLE organization_memberships (
    organization_membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    added_by_user_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    UNIQUE (organization_id, user_id),
    CONSTRAINT fk_organization_memberships_organization
        FOREIGN KEY (organization_id) REFERENCES organizations(organization_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_organization_memberships_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_organization_memberships_added_by
        FOREIGN KEY (added_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE INDEX idx_organization_memberships_user
    ON organization_memberships(user_id, active);

INSERT OR IGNORE INTO organization_memberships
    (organization_id, user_id, added_by_user_id)
SELECT organization_id, user_id, assigned_by_user_id
FROM organization_role_assignments
WHERE active = 1;

INSERT OR IGNORE INTO organization_memberships
    (organization_id, user_id, added_by_user_id)
SELECT course.organization_id, unit_role.user_id,
       unit_role.assigned_by_user_id
FROM unit_role_assignments AS unit_role
JOIN unit_offerings AS offering
  ON offering.unit_offering_id = unit_role.unit_offering_id
JOIN courses AS course ON course.course_id = offering.course_id
WHERE unit_role.active = 1;
