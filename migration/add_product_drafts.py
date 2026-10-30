"""Add the five Raf Decor product drafts supplied in October 2026.

Run from the backend directory with MONGO_URL and DB_NAME set (or in backend/.env):
    python -m migration.add_product_drafts

Products are inserted as unpublished drafts with empty image lists and zero placeholder
prices. Existing products with the same slug are left unchanged.
"""
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from core.db import client, db  # noqa: E402
from core.utils import new_id, now  # noqa: E402
from services.inventory import denormalize  # noqa: E402


DRAFTS = [
    {
        "name": "প্রিমিয়াম মাহগনি কাঠের সার্ভিং ট্রে",
        "slug": "premium-mahogany-serving-tray",
        "short_description": "প্রিমিয়াম মাহগনি কাঠের মার্জিত ট্রে—চা-কফি পরিবেশন, নাশতা সাজানো ও অতিথি আপ্যায়নে প্রাকৃতিক আভিজাত্যের ছোঁয়া।",
        "description": """
<section>
  <p>চা-কফির আড্ডা কিংবা অতিথি আপ্যায়ন—প্রতিটি পরিবেশনকে আরও সুন্দর করে তুলুন RAF DECOR-এর প্রিমিয়াম মাহগনি কাঠের ট্রে দিয়ে। প্রাকৃতিক কাঠের অনন্য গ্রেইন ও মার্জিত নকশা আপনার টেবিলে যোগ করবে উষ্ণ, আভিজাত্যপূর্ণ একটি ছোঁয়া।</p>
  <h3>উপাদান ও মাপ</h3>
  <ul>
    <li><strong>উপাদান:</strong> প্রিমিয়াম মাহগনি কাঠ</li>
    <li><strong>ওভাল ট্রে:</strong> ১০.৫″ × ১৫.৫″</li>
    <li><strong>রাউন্ড ট্রে:</strong> ১০.৫″ × ১০.৫″</li>
  </ul>
  <h3>যে কাজে ব্যবহার করতে পারেন</h3>
  <ul>
    <li>চা-কফি পরিবেশন</li>
    <li>নাশতা ও মিষ্টি সাজিয়ে রাখা</li>
    <li>অতিথি আপ্যায়ন ও ঘরের সাজে প্রাকৃতিক কাঠের ছোঁয়া যোগ করা</li>
  </ul>
  <p>আপনার পরিবেশনকে করে তুলুন আরও বিশেষ—প্রাকৃতিক কাঠের নিজস্ব সৌন্দর্যে।</p>
</section>
""".strip(),
        "materials": "প্রিমিয়াম মাহগনি কাঠ",
        "dimensions": "ওভাল: ১০.৫″ × ১৫.৫″; রাউন্ড: ১০.৫″ × ১০.৫″",
        "specs": [
            {"label": "উপাদান", "value": "প্রিমিয়াম মাহগনি কাঠ"},
            {"label": "ওভাল ট্রে", "value": "১০.৫″ × ১৫.৫″"},
            {"label": "রাউন্ড ট্রে", "value": "১০.৫″ × ১০.৫″"},
        ],
        "tags": ["serving tray", "mahogany", "মাহগনি", "সার্ভিং ট্রে"],
    },
    {
        "name": "উডেন রুটি পট — মাহগনি কাঠ",
        "slug": "mahogany-wooden-roti-pot",
        "short_description": "মাহগনি কাঠের উডেন রুটি পট। ভেতর-বাইরের মাপ ও গভীরতার বিস্তারিত নিচে দেওয়া হলো।",
        "description": """
<section>
  <p>প্রাকৃতিক মাহগনি কাঠে তৈরি উডেন রুটি পট। কাঠের স্বাভাবিক রং ও গ্রেইন আপনার খাবার পরিবেশনের টেবিলে যোগ করবে একটি পরিপাটি, প্রাকৃতিক আবহ।</p>
  <h3>পণ্যের বিবরণ</h3>
  <ul>
    <li><strong>উপাদান:</strong> মাহগনি কাঠ</li>
    <li><strong>ভেতরের ব্যাস:</strong> ৮–১০ ইঞ্চি</li>
    <li><strong>বাইরের ব্যাস:</strong> ১০ ইঞ্চি</li>
    <li><strong>উচ্চতা:</strong> ৩ ইঞ্চি</li>
    <li><strong>ভেতরের গভীরতা:</strong> ২–২.৫ ইঞ্চি</li>
  </ul>
  <p><strong>বিশেষ অফার:</strong> সরবরাহ করা তথ্য অনুযায়ী সাশ্রয় ৳৩৯৯।</p>
</section>
""".strip(),
        "materials": "মাহগনি কাঠ",
        "dimensions": "ভেতরের ব্যাস: ৮–১০″; বাইরের ব্যাস: ১০″; উচ্চতা: ৩″; ভেতরের গভীরতা: ২–২.৫″",
        "specs": [
            {"label": "উপাদান", "value": "মাহগনি কাঠ"},
            {"label": "ভেতরের ব্যাস", "value": "৮–১০ ইঞ্চি"},
            {"label": "বাইরের ব্যাস", "value": "১০ ইঞ্চি"},
            {"label": "উচ্চতা", "value": "৩ ইঞ্চি"},
            {"label": "ভেতরের গভীরতা", "value": "২–২.৫ ইঞ্চি"},
            {"label": "প্রদত্ত অফার", "value": "সাশ্রয় ৳৩৯৯; নিয়মিত ও অফার মূল্য যোগ করার পর যাচাই করতে হবে"},
        ],
        "tags": ["roti pot", "mahogany", "রুটি পট", "মাহগনি"],
    },
    {
        "name": "Full Round Center Table",
        "slug": "full-round-gamari-center-table",
        "short_description": "পাহাড়ি গামারি কাঠের গোল টপ ও আয়রন পায়ায় তৈরি সেন্টার টেবিল।",
        "description": """
<section>
  <p>প্রাকৃতিক পাহাড়ি গামারি কাঠের গোল টপ এবং মজবুত আয়রন পায়ার সমন্বয়ে তৈরি Full Round Center Table। কাঠের স্বাভাবিক গ্রেইন ও সরল নকশা বসার ঘরের সেন্টার টেবিল হিসেবে মানিয়ে যাবে।</p>
  <h3>পণ্যের বিবরণ</h3>
  <ul>
    <li><strong>টপের উপাদান:</strong> পাহাড়ি গামারি কাঠ</li>
    <li><strong>পায়া:</strong> আয়রন</li>
    <li><strong>পায়ার ব্যাস:</strong> ১.৫ ইঞ্চি</li>
    <li><strong>প্রদত্ত মাপ:</strong> ২৪″ × ২৪″</li>
  </ul>
</section>
""".strip(),
        "materials": "পাহাড়ি গামারি কাঠের টপ, আয়রন পায়া",
        "dimensions": "প্রদত্ত মাপ: ২৪″ × ২৪″; আয়রন পায়ার ব্যাস: ১.৫″",
        "specs": [
            {"label": "টপের উপাদান", "value": "পাহাড়ি গামারি কাঠ"},
            {"label": "পায়া", "value": "আয়রন"},
            {"label": "পায়ার ব্যাস", "value": "১.৫ ইঞ্চি"},
            {"label": "প্রদত্ত মাপ", "value": "২৪″ × ২৪″"},
        ],
        "tags": ["center table", "gamari wood", "সেন্টার টেবিল", "গামারি কাঠ"],
    },
    {
        "name": "Premium Shishu Wood Bowl & Plate Set",
        "slug": "premium-shishu-wood-bowl-plate-set",
        "short_description": "প্রিমিয়াম শিশু কাঠের বড় বাটি, কাঠের প্লেট ও মিনি বাটির সেট—প্রাকৃতিক গ্রেইন ও আকর্ষণীয় পলিশে।",
        "description": """
<section>
  <p>RAF DECOR-এর Premium Shishu Wood Bowl &amp; Plate Set-এ রয়েছে প্রাকৃতিক কাঠের অনন্য গ্রেইন, আকর্ষণীয় পলিশ এবং এলিগ্যান্ট ডিজাইনের সমন্বয়। ফল, স্ন্যাকস ও অন্যান্য খাবার পরিবেশনে আপনার টেবিলকে করে তুলুন আরও নান্দনিক।</p>
  <h3>সেটের পণ্য ও মাপ</h3>
  <ul>
    <li><strong>বড় বাটি:</strong> ব্যাস ৭–৮ ইঞ্চি, উচ্চতা ৩ ইঞ্চি, ভেতরের গভীরতা ২.৫–২.৮ ইঞ্চি</li>
    <li><strong>কাঠের প্লেট:</strong> ব্যাস ৯–১০ ইঞ্চি, উচ্চতা ১.৫ ইঞ্চি</li>
    <li><strong>মিনি বাটি:</strong> ব্যাস ৫ ইঞ্চি, উচ্চতা ৩ ইঞ্চি</li>
    <li><strong>উপাদান:</strong> প্রিমিয়াম শিশু কাঠ</li>
  </ul>
  <p>নিজের ঘরের জন্য কিংবা প্রিয়জনকে উপহার দিতে বেছে নিন প্রাকৃতিক কাঠের এই সেট।</p>
</section>
""".strip(),
        "materials": "প্রিমিয়াম শিশু কাঠ",
        "dimensions": "বড় বাটি: ব্যাস ৭–৮″, উচ্চতা ৩″, গভীরতা ২.৫–২.৮″; প্লেট: ব্যাস ৯–১০″, উচ্চতা ১.৫″; মিনি বাটি: ব্যাস ৫″, উচ্চতা ৩″",
        "specs": [
            {"label": "উপাদান", "value": "প্রিমিয়াম শিশু কাঠ"},
            {"label": "বড় বাটি", "value": "ব্যাস ৭–৮″; উচ্চতা ৩″; ভেতরের গভীরতা ২.৫–২.৮″"},
            {"label": "কাঠের প্লেট", "value": "ব্যাস ৯–১০″; উচ্চতা ১.৫″"},
            {"label": "মিনি বাটি", "value": "ব্যাস ৫″; উচ্চতা ৩″"},
        ],
        "tags": ["bowl plate set", "shishu wood", "বাটি প্লেট সেট", "শিশু কাঠ"],
    },
    {
        "name": "Natural Wood Stool",
        "slug": "natural-shishu-wood-stool",
        "short_description": "প্রিমিয়াম শিশু কাঠের স্টুল—বাড়ি ও অফিসে ব্যবহারের জন্য যত্নসহকারে তৈরি।",
        "description": """
<section>
  <p>RAF DECOR-এর Natural Wood Stool প্রাকৃতিক কাঠের সৌন্দর্য ও আভিজাত্যকে সহজ, রুচিশীল নকশায় তুলে ধরে। যত্নসহকারে তৈরি এই স্টুল বাড়ি কিংবা অফিসের জন্য উপযোগী।</p>
  <h3>পণ্যের বিবরণ</h3>
  <ul>
    <li><strong>উপাদান:</strong> প্রিমিয়াম শিশু কাঠ</li>
    <li><strong>টপের মাপ:</strong> ১৪″ × ১৪″</li>
    <li><strong>পায়া:</strong> ২ ইঞ্চি গোল</li>
    <li><strong>উচ্চতা:</strong> ১৮ ইঞ্চি</li>
  </ul>
  <h3>বৈশিষ্ট্য</h3>
  <ul>
    <li>সুন্দর ও স্বতন্ত্র নকশা</li>
    <li>মজবুত ও দীর্ঘস্থায়ী ব্যবহারের জন্য তৈরি</li>
    <li>বাড়ি ও অফিস—দুই জায়গার জন্য উপযোগী</li>
  </ul>
</section>
""".strip(),
        "materials": "প্রিমিয়াম শিশু কাঠ",
        "dimensions": "টপ: ১৪″ × ১৪″; গোল পায়া: ২″; উচ্চতা: ১৮″",
        "specs": [
            {"label": "উপাদান", "value": "প্রিমিয়াম শিশু কাঠ"},
            {"label": "টপ", "value": "১৪″ × ১৪″"},
            {"label": "গোল পায়া", "value": "২ ইঞ্চি"},
            {"label": "উচ্চতা", "value": "১৮ ইঞ্চি"},
        ],
        "tags": ["stool", "shishu wood", "স্টুল", "শিশু কাঠ"],
    },
]


async def add_drafts():
    added = 0
    skipped = 0
    for draft in DRAFTS:
        product = {
            **draft,
            "category_ids": [],
            "status": "draft",
            "publish_at": None,
            "featured": False,
            "sku": None,
            "price": 0,
            "sale_price": None,
            "manage_stock": False,
            "stock_quantity": None,
            "stock_status": "in_stock",
            "low_stock_threshold": 2,
            "attributes": [],
            "variants": [],
            "images": [],
            "shipping_charge": 0,
            "seo": {"title": draft["name"][:120], "description": draft["short_description"][:320]},
            "sort_order": 0,
            "views": 0,
            "sales_count": 0,
            "created_at": now(),
            "updated_at": now(),
            "created_by": "product_draft_import",
        }
        denormalize(product)
        result = await db.products.update_one(
            {"slug": draft["slug"]},
            {"$setOnInsert": {"id": new_id(), **product}},
            upsert=True,
        )
        if result.upserted_id is None:
            skipped += 1
            print(f"Skipped existing product: {draft['slug']}")
        else:
            added += 1
            print(f"Added draft: {draft['slug']}")
    print(f"Done. Added {added} draft(s); skipped {skipped} existing slug(s).")


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(add_drafts())
    finally:
        client.close()
