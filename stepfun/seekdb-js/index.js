// Minimal seekdb-js compatible client used by this demo.
// The published seekdb-js package is not available in the competition image;
// this module keeps the same Promise-based surface and talks to seekdb over
// the MySQL wire protocol (seekdb is MySQL compatible).
import mysql from 'mysql2/promise';

export class SeekDB {
  constructor(config = {}) {
    this.config = {
      host: process.env.SEEKDB_HOST || '127.0.0.1',
      port: Number(process.env.SEEKDB_PORT || 2881),
      user: process.env.SEEKDB_USER || 'root',
      password: process.env.SEEKDB_PASSWORD || '',
      database: process.env.SEEKDB_DATABASE || 'stepfun_kb',
      ...config,
    };
    this.pool = null;
  }

  async connect() {
    if (this.pool) return this;
    const { database, ...base } = this.config;
    const admin = await mysql.createConnection(base);
    await admin.query(`CREATE DATABASE IF NOT EXISTS \`${database}\``);
    await admin.end();
    this.pool = mysql.createPool({ ...this.config, waitForConnections: true, connectionLimit: 8 });
    return this;
  }

  async query(sql, params = []) {
    await this.connect();
    const [rows] = await this.pool.query(sql, params);
    return rows;
  }

  async close() {
    if (this.pool) await this.pool.end();
    this.pool = null;
  }
}

export default SeekDB;
