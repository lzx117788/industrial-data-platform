import csv
import json
import sqlite3
from datetime import timedelta

from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.models import Source, utcnow
from app.schemas import iso


def seed_demo(db: Database, settings: Settings):
    root = settings.import_root
    root.mkdir(parents=True, exist_ok=True)
    base = utcnow() - timedelta(hours=2)

    def stamp(minutes):
        return iso(base + timedelta(minutes=minutes))

    definitions = [
        dict(
            code="asset-registry",
            name="设备档案 · CSV",
            kind="csv",
            entity="device",
            config={"path": "devices.csv"},
        ),
        dict(
            code="alarm-feed",
            name="实时告警 · HTTP",
            kind="http",
            entity="alarm",
            config={"url": settings.demo_base_url + "/demo-source/alarms"},
        ),
        dict(
            code="maintenance-db",
            name="维修记录 · SQL",
            kind="sqlite",
            entity="maintenance",
            config={"path": "maintenance.db", "table": "maintenance_records"},
        ),
    ]
    with db.session() as session:
        for definition in definitions:
            if session.scalar(select(Source).where(Source.code == definition["code"])) is None:
                session.add(Source(**definition))
        session.commit()

    if not (root / "devices.csv").exists():
        devices = []
        for i, (name, category, location) in enumerate(
            [
                ("一号空压机", "空压机", "A 车间 / 动力区"),
                ("循环冷却泵", "水泵", "B 车间 / 冷却区"),
                ("离心分离机", "离心机", "A 车间 / 生产区"),
            ],
            1,
        ):
            devices.append(
                dict(
                    external_id=f"EQ-{i:03d}",
                    name=name,
                    category=category,
                    location=location,
                    status="active",
                    updated_at=stamp(i),
                    aliases=json.dumps(
                        {"alarm-feed": f"PLC-A{i:02d}", "maintenance-db": f"M-{1000 + i}"}
                    ),
                )
            )
        devices.append(devices[0].copy())
        devices.append(
            dict(
                external_id="EQ-004",
                name="",
                category="风机",
                location="C 车间",
                status="active",
                updated_at=stamp(4),
                aliases="{}",
            )
        )
        with (root / "devices.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(devices[0]))
            writer.writeheader()
            writer.writerows(devices)

    if not (root / "alarms.json").exists():
        alarms = [
            dict(
                external_id="A-001",
                device_ref="PLC-A01",
                code="TEMP_HIGH",
                severity="HIGH",
                message="排气温度超过阈值",
                occurred_at=stamp(30),
                updated_at=stamp(31),
            ),
            dict(
                external_id="A-002",
                device_ref="PLC-A02",
                code="PRESSURE_LOW",
                severity="WARN",
                message="冷却管路压力偏低",
                occurred_at=stamp(35),
                updated_at=stamp(36),
            ),
            dict(
                external_id="A-003",
                device_ref="PLC-A01",
                code="VIBRATION",
                severity="WARN",
                message="振动趋势上升",
                occurred_at=stamp(40),
                updated_at=stamp(41),
            ),
            dict(
                external_id="A-004",
                device_ref="PLC-UNKNOWN",
                code="OFFLINE",
                severity="WARN",
                message="未知设备离线",
                occurred_at=stamp(45),
                updated_at=stamp(46),
            ),
            dict(
                external_id="A-005",
                device_ref="PLC-A03",
                code="TEST",
                severity="INVALID",
                message="异常等级示例",
                occurred_at=stamp(50),
                updated_at=stamp(51),
            ),
        ]
        (root / "alarms.json").write_text(
            json.dumps({"items": alarms}, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if not (root / "maintenance.db").exists():
        with sqlite3.connect(root / "maintenance.db") as connection:
            connection.execute(
                "CREATE TABLE maintenance_records (external_id TEXT PRIMARY KEY, device_ref TEXT, description TEXT, technician TEXT, cost TEXT, performed_at TEXT, updated_at TEXT)"
            )
            connection.executemany(
                "INSERT INTO maintenance_records VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        "R-001",
                        "M-1001",
                        "更换进气滤芯并检查润滑油",
                        "张工",
                        "450.00",
                        stamp(10),
                        stamp(11),
                    ),
                    (
                        "R-002",
                        "M-1002",
                        "检查机械密封并紧固管路",
                        "李工",
                        "280.00",
                        stamp(20),
                        stamp(21),
                    ),
                    (
                        "R-003",
                        "M-1003",
                        "负数费用质量检查示例",
                        "王工",
                        "-30.00",
                        stamp(25),
                        stamp(26),
                    ),
                ],
            )
