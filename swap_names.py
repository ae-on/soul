from db.connection import get_connection

def swap_names():
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id, full_name FROM users WHERE full_name IS NOT NULL AND full_name != ''")
        users = cur.fetchall()
        
        count = 0
        for u in users:
            # u[0] - это id, u[1] - это full_name (так как это кортеж)
            user_id = u[0]
            full_name = u[1]
            
            parts = full_name.strip().split()
            if len(parts) == 2:
                # Меняем местами (Фамилия Имя)
                new_name = f"{parts[1]} {parts[0]}"
                cur.execute("UPDATE users SET full_name = %s WHERE id = %s", (new_name, user_id))
                count += 1
        conn.commit()
    print(f"✅ Обновлено имен: {count}")

if __name__ == "__main__":
    swap_names()
