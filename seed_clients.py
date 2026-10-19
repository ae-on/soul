import random
from datetime import datetime, timedelta
from db.connection import get_connection
from db.models import User, UserRole, Subscription, SubscriptionType

def seed():
    first_names = ["Александр", "Елена", "Дмитрий", "Ольга", "Сергей", "Анна", "Иван", "Мария", "Олег", "Татьяна"]
    last_names = ["Иванов", "Смирнова", "Кузнецов", "Попова", "Васильев", "Новикова", "Морозов", "Волкова", "Соколов", "Михайлова"]
    
    now = datetime.now()
    purchased_at = now.strftime('%Y-%m-%d %H:%M:%S')
    expires_at = (now + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')

    print("Создаем клиентов...")
    for i in range(10):
        fn = f"{last_names[i]} {first_names[i]}"
        tg_id = 10000000 + i  # Фейковые Telegram ID
        uname = f"test_user_{i}"
        
        # Создаем юзера (если он уже есть, вернет существующего)
        user = User.create_or_get(tg_id, uname)
        if not user:
            print(f"❌ Не удалось создать {fn}")
            continue
            
        uid = user['id']
        
        # Обновляем ФИО и статус
        User.update_contacts(uid, fn, f"+37529123456{i}")
        User.set_status(uid, 'active')
        User.set_consent(uid, "1.0")
        
        # Выдаем роль student
        UserRole.grant(uid, 'student')
        
        # Выдаем случайный абонемент (например, только четным)
        if i % 2 == 0:
            sub_type = SubscriptionType.get_by_id(5) # Стандарт 8 часов
            if sub_type:
                Subscription.create(
                    user_id=uid,
                    type_id=5,
                    purchased_at=purchased_at,
                    started_at=purchased_at,
                    expires_at=expires_at,
                    hours_total=sub_type['hours_count'],
                    hours_left=sub_type['hours_count'],
                    status='active'
                )
        print(f"✅ {fn} (TG: {tg_id})")

if __name__ == "__main__":
    seed()
