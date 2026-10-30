"""Seeds site settings with content taken from the live rafdecor.shop homepage/footer (only inserted if missing)."""
from core.db import db
from core.utils import now

DISTRICTS = ["Bagerhat", "Bandarban", "Barguna", "Barishal", "Bhola", "Bogura", "Brahmanbaria", "Chandpur", "Chapai Nawabganj", "Chattogram",
             "Chuadanga", "Cox's Bazar", "Cumilla", "Dhaka", "Dinajpur", "Faridpur", "Feni", "Gaibandha", "Gazipur", "Gopalganj", "Habiganj",
             "Jamalpur", "Jashore", "Jhalokati", "Jhenaidah", "Joypurhat", "Khagrachhari", "Khulna", "Kishoreganj", "Kurigram", "Kushtia",
             "Lakshmipur", "Lalmonirhat", "Madaripur", "Magura", "Manikganj", "Meherpur", "Moulvibazar", "Munshiganj", "Mymensingh", "Naogaon",
             "Narail", "Narayanganj", "Narsingdi", "Natore", "Netrokona", "Nilphamari", "Noakhali", "Pabna", "Panchagarh", "Patuakhali",
             "Pirojpur", "Rajbari", "Rajshahi", "Rangamati", "Rangpur", "Satkhira", "Shariatpur", "Sherpur", "Sirajganj", "Sunamganj",
             "Sylhet", "Tangail", "Thakurgaon"]

SITE = {
    "key": "site",
    "brand": {"name": "Raf Decor", "tagline": "Handcrafted with love", "logo": "https://rafdecor.shop/wp-content/uploads/2026/08/Logo.png"},
    "announcement": {"enabled": True, "text": "প্রাকৃতিক ও উন্নতমানের কাঠ দিয়ে তৈরি — Premium handcrafted wooden furniture & kitchenware"},
    "hero": {"eyebrow": "Premium Wooden Masterpieces", "title": "Timeless Design, Artisanal Soul",
             "subtitle": "প্রাকৃতিক ও উন্নতমানের কাঠ দিয়ে তৈরি আমাদের প্রিমিয়াম ফার্নিচার ও গৃহসজ্জার সামগ্রী। প্রতিটি কাঠের আসবাবে রয়েছে মাস্টার ফিনিশিং এবং স্থায়িত্বের নিশ্চয়তা।",
             "cta_label": "Explore Collection", "cta_link": "/shop", "image": None},
    "philosophy": {"eyebrow": "Our Philosophy", "title": "The Raf Decor Standard", "pillars": [
        {"title": "প্রিমিয়াম উড সোর্সিং", "body": "প্রতিটি প্রডাক্ট শতভাগ প্রাকৃতিক এবং উন্নতমানের মেহগনি, রেইনট্রি ও সেগুন কাঠ থেকে তৈরি করা হয়।"},
        {"title": "নিখুঁত কারুকাজ", "body": "আমাদের অভিজ্ঞ কারিগরদের নিপুণ হাতের ছোঁয়ায় প্রতিটি কাঠে ফুটিয়ে তোলা হয় মসৃণ ও দৃষ্টিনন্দন ফিনিশিং।"},
        {"title": "স্থায়িত্বের নিশ্চয়তা", "body": "উন্নতমানের প্রলেপ ও ট্রিটমেন্ট ব্যবহারের ফলে আমাদের প্রতিটি আসবাব বছরের পর বছর থাকে একদম নতুনের মতো।"}]},
    "home": {"collection_title": "আমাদের সিগনেচার কালেকশন", "collection_eyebrow": "Exquisite Creations",
             "cta_title": "অভিজাত গৃহসজ্জা ও লাইফস্টাইল", "cta_body": "আপনার ডাইনিং টেবিল বা লিভিং রুমের সৌন্দর্যকে ফুটিয়ে তুলতে আমাদের প্রিমিয়াম কালেকশন আজই ঘুরে দেখুন।", "cta_label": "সকল প্রডাক্ট দেখুন"},
    "footer": {"about": "We source premium, eco-friendly timbers and craft sustainable designs to bring warmth, organic lines, and modern utility to your space."},
    "contact": {"address": "সাঈদনগর জিরো পয়েন্ট, ভাটারা, ঢাকা", "phone": "01904758169", "phone_display": "০১৯০৪-৭৫৮১৬৯", "email": "Rafdecor.official@gmail.com",
                "hours": "১০:০০ AM – ০৮:০০ PM", "whatsapp": "8801904758169"},
    "social": {"facebook": ""},
    "faqs": [],
    "seo": {"title": "Raf Decor | Premium Handcrafted Wooden Furniture & Kitchenware", "description": "Handcrafted with love — premium wooden furniture, kitchenware and home decor made from natural mahogany, raintree and teak wood in Bangladesh."},
    "shipping": {"mode": "sum", "free_over": None, "note": "Delivery charge is set per product (as on the current checkout)."},
    "payments": {"enabled": {"cod": True, "bank_transfer": True, "sslcommerz": False},
                 "cod_instructions": "Pay with cash upon delivery.",
                 "bank_instructions": "Make your payment directly into our bank account. Please use your Order ID as the payment reference. Your order will not be shipped until the funds have cleared in our account.",
                 "bank_details": ""},
    "districts": DISTRICTS,
}


async def seed_settings():
    existing = await db.settings.find_one({"key": "site"})
    if not existing:
        await db.settings.insert_one({**SITE, "created_at": now()})
    else:
        missing = {k: v for k, v in SITE.items() if k not in existing}
        if missing:
            await db.settings.update_one({"key": "site"}, {"$set": missing})
